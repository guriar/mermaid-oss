"""hb.run.remote - the heavy gates run on the remote runner, one workflow_dispatch per tip.

The workflow is the product's, named in `gates.remote.workflow`; there is no default.

The dispatch carries the run id and the exact tip; the journal records the workflow run it
found; the artifact comes down whole and holds one TRUE_EXIT line per gate beside the
provenance the workflow wrote. A result is accepted only when that provenance names this run
and this tip. Five ways the boxes can fail to answer each read `remote gate: <mode>` and are
UNMEASURED by name, never red, never green and never rerun: a lost acknowledgement, a
duplicate dispatch, a partial artifact, a foreign tip, a timeout. The local command runs only
when GitHub or the boxes cannot be reached, and the gate line says so. GitHub is read through
`common.gh`, the one client ci-watch reads it through.
"""
import json
import time

from hb.rig.common import gh, state_dir
from hb.gates.gates import _NODEID_RE, read_true_exit
from hb.rig import gitops

POLL_S = 30
ACK_S = 180
DEFAULTS = {"gates": [], "timeout_min": 45}
FALLBACK = "remote gate: ran locally, the remote runner is down"


def settings(c):
    """The `gates.remote` block over the engine's own defaults, or None when no gate is remote."""
    block = (c or {}).get("gates")
    block = block.get("remote") if isinstance(block, dict) else None
    return {**DEFAULTS, **block} if isinstance(block, dict) else None


def names(c):
    """The gate names the boxes run."""
    s = settings(c)
    return set(s["gates"]) if s else set()


def available(c):
    """(True, None) when an online runner answers for the repo; (False, why) when GitHub or
    the boxes cannot be reached. Only that answer sends a remote gate to the local command."""
    rc, o, e = gh(["api", f"repos/{c['repo']}/actions/runners", "--jq",
                   '[.runners[] | select(.status == "online")] | length'])
    if rc != 0:
        return False, f"GitHub did not answer the runner list: {(e or o).strip()[:160]}"
    try:
        online = int(o.strip().splitlines()[-1])
    except (ValueError, IndexError):
        online = 0
    return (True, None) if online else (False, "no self-hosted runner is online")


def _title(run_id, tip):
    return f"harness-gates {run_id} {tip}"


def _artifact(run_id, tip):
    return f"harness-gates-{run_id}-{tip}"


def _unmeasured(gates, why):
    return [{"name": g["name"], "cmd": ["remote", *g["cmd"]], "true_exit": None, "artifact": None, "red_ids": [],
             "unmeasured": f"remote gate: {why}"} for g in gates]


def _find_runs(c, s, title, deadline):
    """The workflow runs titled for this run id and tip, polled until one appears or the deadline."""
    while True:
        rc, o, _ = gh(["api", f"repos/{c['repo']}/actions/workflows/{s['workflow']}/runs?event=workflow_dispatch&per_page=50",
                       "--jq", "[.workflow_runs[] | {id, display_title, status, conclusion, html_url}]"])
        runs = [r for r in (json.loads(o) if rc == 0 and o.strip() else []) if r.get("display_title") == title]
        if runs or time.time() >= deadline:
            return runs
        time.sleep(POLL_S)


def _workflow_run(j, c, s, wt, tip):
    """(the workflow run id for this tip, None) or (None, why). One dispatch per tip, journaled
    before it is sent, so a resume looks for the run it may have started and never sends a second."""
    for e in reversed(list(j.events())):
        if e.get("kind") == "remote-run" and e.get("tip") == tip:
            return e["workflow_run"], None
    if not any(e.get("kind") == "remote-dispatch" and e.get("tip") == tip for e in j.events()):
        branch = gitops.git(wt, "rev-parse", "--abbrev-ref", "HEAD")[1].strip()
        # The boxes check out the branch by name, so the tip must be on origin before the dispatch.
        pushed = gitops.push(j, wt, branch, False, "remote-push")
        if pushed.get("rc"):
            return None, f"lost acknowledgement: the branch did not push before the dispatch: {(pushed.get('err') or '').strip()[:160]}"
        j.append("remote-dispatch", tip=tip, branch=branch, workflow=s["workflow"], run_id=j.run_id)
        rc, o, e = gh(["api", "-X", "POST", f"repos/{c['repo']}/actions/workflows/{s['workflow']}/dispatches",
                       "-f", f"ref={branch}", "-f", f"inputs[run_id]={j.run_id}", "-f", f"inputs[tip]={tip}"], timeout=60)
        if rc != 0:
            return None, f"lost acknowledgement: the dispatch answered {rc}: {(e or o).strip()[:160]}"
    runs = _find_runs(c, s, _title(j.run_id, tip), time.time() + ACK_S)
    if not runs:
        return None, f"lost acknowledgement: no workflow run named this run id and tip appeared within {ACK_S} s"
    if len(runs) > 1:
        return None, "duplicate dispatch: workflow runs " + ", ".join(str(r["id"]) for r in runs) + " all name this run id and tip"
    j.append("remote-run", tip=tip, workflow_run=runs[0]["id"], url=runs[0].get("html_url"))
    return runs[0]["id"], None


def _wait(c, s, run_id):
    """The run's final state, or None past `timeout_min`."""
    deadline = time.time() + float(s["timeout_min"]) * 60
    while True:
        rc, o, _ = gh(["api", f"repos/{c['repo']}/actions/runs/{run_id}", "--jq", "{status, conclusion}"])
        st = json.loads(o) if rc == 0 and o.strip() else {}
        if st.get("status") == "completed":
            return st
        if time.time() >= deadline:
            return None
        time.sleep(POLL_S)


def _provenance(path):
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []:
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _rows(j, c, s, slug, tip, run_id, gates):
    """The gate rows read from the run's artifact, or every gate UNMEASURED by the one reason."""
    st = _wait(c, s, run_id)
    if st is None:
        return _unmeasured(gates, f"timeout: workflow run {run_id} not complete after {s['timeout_min']} minutes")
    into = state_dir(c) / "evidence" / str(slug).lstrip("#") / "gates" / f"remote-{run_id}"
    if not (into / "provenance.txt").exists():
        into.mkdir(parents=True, exist_ok=True)
        rc, o, e = gh(["run", "download", str(run_id), "--repo", c["repo"], "--name", _artifact(j.run_id, tip),
                       "--dir", str(into)], timeout=900)
        if rc != 0:
            return _unmeasured(gates, f"partial artifact: none came down from workflow run {run_id} "
                                      f"({st.get('conclusion')}): {(e or o).strip()[:160]}")
    prov = _provenance(into / "provenance.txt")
    if not prov:
        return _unmeasured(gates, f"partial artifact: no provenance in workflow run {run_id}")
    if (prov.get("RUN_ID"), prov.get("TIP"), prov.get("HEAD")) != (j.run_id, tip, tip):
        return _unmeasured(gates, f"foreign tip: workflow run {run_id} measured run {prov.get('RUN_ID')} at "
                                  f"{prov.get('TIP')} (HEAD {prov.get('HEAD')}), this round asked for {j.run_id} at {tip}")
    rows = []
    for g in gates:
        f = into / f"{g['name']}.txt"
        text = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
        found = read_true_exit(text)
        if found is None:
            return _unmeasured(gates, f"partial artifact: no TRUE_EXIT line for {g['name']} in workflow run {run_id}")
        rows.append({"name": g["name"], "cmd": ["remote", *g["cmd"]], "true_exit": found, "artifact": str(f),
                     "red_ids": sorted({x.replace("\\", "/") for x in _NODEID_RE.findall(text)})[:40],
                     "remote_run": run_id, **({"outcome": g["outcome"]} if g.get("outcome") else {})})
    return rows


def measure(j, c, wt, slug, tip, gates):
    """One row per gate, all from the one workflow run at `tip`."""
    s = settings(c)
    t0 = time.time()
    run_id, why = (_workflow_run(j, c, s, wt, tip) if s.get("workflow")
                   else (None, "gates.remote.workflow is not set in harness-config.json"))
    rows = _rows(j, c, s, slug, tip, run_id, gates) if run_id is not None else _unmeasured(gates, why)
    t1 = time.time()
    j.append("remote-result", tip=tip, workflow_run=run_id,
             gates=[{"name": r["name"], "true_exit": r["true_exit"], "unmeasured": r.get("unmeasured")} for r in rows])
    stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    return [dict(r, started=stamp(t0), ended=stamp(t1), seconds=round(t1 - t0, 1)) for r in rows]
