"""hb.run.gating - the gates a run measures itself by.

Which gates there are is decided by two sources only: the product config (`gates.commands`
and the boxes' `gates.remote` names) and ciparity's read of CI's own workflows. A gate the
config marks `"at": "every-tip"` runs at every tip a PR carries. Every round measures every
gate at its tip, each through `hb run-gate` so the result is the artifact's own TRUE_EXIT line
(gates.read_true_exit). Every gate is admitted by hb.admission, a gate marked `"database":
true` runs on the run's own leased database when the venue offers `database`, and a red is
settled before any seat is spent on it: read into two facts (`verdict`), rerun once after an
environment fault's recovery, its failing test files run alone through the gate's `isolate`
command, or replayed at the base commit. Only the runner proves a red pre-existing; a fixer's
word never does.
"""

import os
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from hb.rig.common import branch_setting
from hb.venue import adapter
from hb.venue import machine
from hb.venue import admission
from hb.gates.gates import GATE_NAME_ENV, read_true_exit
from hb.gates import ciparity
from hb.contract import policy
from hb.gates import remote
from hb.runner import stops as stop_ids
from hb.rig import gitops
from hb.runner import hbcall
from hb.gates import verdict


NO_RESULT = "no TRUE_EXIT line"


class RigFault(Exception):
    """The runner can no longer measure: its tree's tip is unreadable, or no gate of a round
    printed a result. The door stops the run on it; it is never a red handed to a fixer."""

    def __init__(self, journal, why):
        super().__init__(why)
        self.journal = journal
        self.why = why


def lost_tools(results):
    """True when every gate of a round that was meant to run printed no TRUE_EXIT line."""
    ran = list(results or [])
    return bool(ran) and all(r.get("true_exit") is None and r.get("unmeasured") == NO_RESULT for r in ran)


def parallel(c):
    """How many local gates run at once: the host's to say (`gates.parallel`), 3 by default."""
    block = (c or {}).get("gates")
    return int((block if isinstance(block, dict) else {}).get("parallel") or 3)


def _as_argv(cmd):
    """A gate's command as an argv: the config may write it as a list or as one shell line."""
    return shlex.split(cmd, posix=(os.name != "nt")) if isinstance(cmd, str) else list(cmd or [])


def default_gates(changed, c):
    """The gates the product config names: `gates` (or `gates.commands`), and every gate the
    boxes run (`gates.remote`). A remote gate is heavy: it runs once per tip, on the boxes; its
    local command, when the config names one, is the fallback for the boxes down. A gate marked
    `"at": "every-tip"` is heavy too: it runs once at every tip the PR carries."""
    configured = c.get("gates")
    if isinstance(configured, dict):
        configured = configured.get("commands")
    gates = [dict(g, cmd=_as_argv(g.get("cmd"))) if isinstance(g, dict) else {"name": " ".join(g), "cmd": list(g)}
             for g in configured or []]
    gates = [dict(g, heavy=True) if g.get("at") == "every-tip" else g for g in gates]
    named = {g["name"] for g in gates}
    gates += [{"name": n, "cmd": []} for n in sorted(remote.names(c)) if n not in named]
    return [dict(g, remote=True, heavy=True) if g["name"] in remote.names(c) else g for g in gates]


def beat_of(j):
    """The run's beat from its opening line: a hotfix, a slice or a wave."""
    return next((e.get("beat") for e in j.events() if e.get("kind") == "phase" and e.get("phase") == "opened"), None) or "hotfix"


def ci_parity(wt, c):
    """The CI checks this tree's own workflows name, derived by ciparity; never a list here."""
    landing = branch_setting(c, "landing")
    return ciparity.derive(wt, landing, ciparity.prefixes(c))


def round_list(wt, changed, c):
    """Every gate a round may run: the configured ones and CI's own scripts."""
    return default_gates(changed, c) + ci_parity(wt, c)["gates"]


def _stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def measure(j, wt, slug, base_sha, changed, c, round_key, heavy=False, only=None):
    """One round of gates. The fast ones (lint, types, changed tests, the CI-parity scripts)
    run after every edit, `gates.parallel` at a time, reported in their declared order; the
    heavy one, the backend suite, runs once after the last edit. Each row carries its own clock and
    its verdict; an environment fault is rerun once after its recovery, never handed to a
    fix seat. The journal key carries the tip, so a resume re-measures only a tree that
    moved, and the answers that name a gate of the round, so an answered gate stop runs its
    gate again."""
    beat = beat_of(j)
    round_gates = [g for g in (only if only is not None else round_list(wt, changed, c))
                   if bool(g.get("heavy")) == heavy and (not g.get("beats") or beat in g["beats"])]
    answers = stop_ids.answers(j)
    answered = sorted(f"{sid}: {answers[sid.casefold()]}" for sid in {stop_ids.gate_id(g["name"]) for g in round_gates}
                      if sid.casefold() in answers)

    def call(g, cmd, cwd, genv):
        t0 = time.time()
        row = {"name": g["name"], "cmd": cmd, "true_exit": None, "artifact": None, "red_ids": [],
               "ci_parity": bool(g.get("ci_parity"))}
        if g.get("outcome"):
            row["outcome"] = g["outcome"]  # the line that says this gate's runner reached its end
        try:
            rc, js, text = hbcall.hb(["run-gate", "--issue", slug, "--", *cmd], cwd=cwd, timeout=policy.GATE_MIN * 60,
                                     env=genv)
            got = read_true_exit(text)
            # run-gate names its artifact and each new red on its own text lines;
            # the fixer reads both from here instead of re-running the gate.
            art = re.search(r"^GATE_OUTPUT=(.+)$", text, re.M)
            reds = re.findall(r"^RED_NODEID=(.+)$", text, re.M)
            row.update(true_exit=got,
                       artifact=art.group(1).strip() if art else (js.get("artifact") or js.get("outfile")),
                       red_ids=[r.strip() for r in reds][:40], unmeasured=None if got is not None else NO_RESULT)
        except subprocess.TimeoutExpired:
            row["unmeasured"] = f"timed out after {policy.GATE_MIN} minutes with no result"
        t1 = time.time()
        return dict(row, started=_stamp(t0), ended=_stamp(t1), seconds=round(t1 - t0, 1))

    def admitted(g, cmd, cwd, genv, holder):
        # a gate starts when the host's free memory is above the line, one start at a time across
        # every run on the host. A gate marked `fans_out` gets as many workers as the free memory
        # above the line pays for, written where its command names `{workers}`
        sized = (2, os.cpu_count() or 2) if g.get("fans_out") else None
        with admission.take(f"gate:{g['name']}", holder, c, j.append, units=sized) as grant:
            if sized:
                workers = str(grant.get("units") or sized[1])
                cmd = [str(x).replace("{workers}", workers) for x in cmd]
            return call(g, cmd, cwd, {**genv, **(grant.get("env") or {})})

    def lease_into(genv):
        lease = lease_with_retry(wt, slug, note=j.append)
        if lease.get("url"):
            genv["DATABASE_URL"] = lease["url"]
        return lease

    def one(g):
        cmd = list(g["cmd"])
        if not cmd:
            # a gate only the boxes run, with no local command in the product config
            return {"name": g["name"], "cmd": [], "true_exit": None, "red_ids": [],
                    "unmeasured": "remote gate: the boxes are down and the product config names no local command for it"}
        cwd = Path(wt) / g["cwd"] if g.get("cwd") else Path(wt)
        genv = dict(g.get("env") or {}, **{GATE_NAME_ENV: g["name"]})
        # CI gives each job its own database; parallel runs here share one server, so a CI step
        # that reads a database gets this run's lease in place of the workflow's url
        leased = bool(g.get("database")) or bool(g.get("ci_parity") and genv.get("DATABASE_URL"))
        holder = f"{j.run_id} {g['name']}"
        # the room a gate needs is admission's to decide, from its measured peak
        if leased:
            # The gate runs on this run's own leased database, so two runs never share one
            # and a base built any other way never reads as a red; released when the run ends.
            # A venue without `database` leases nothing and the gate runs as configured.
            lease = lease_into(genv)
            if lease.get("why") == adapter.not_offered("database"):
                j.append("test-db", gate=g["name"], why=lease["why"])
                leased = False
            elif not lease.get("url"):
                return {"name": g["name"], "true_exit": None,
                        "unmeasured": f"test database lease refused: {lease.get('why') or lease}"}
        row = admitted(g, cmd, cwd, genv, holder)
        first = verdict.classify(row)
        j.append("verdict", key=round_key, gate=g["name"], attempt=1, **first)
        if not first["recovery"]:
            ids = list(row.get("red_ids") or []) if g.get("isolate") and verdict.is_fixable(row) else []
            if ids:
                # a test red in the whole gate and green alone failed by its order or its load:
                # the gate measured nothing the product owns, and it is never read as green
                alone = admitted(g, isolate_argv(g["isolate"], ids), cwd, genv, holder)
                j.append("gate-alone", key=round_key, gate=g["name"], ids=ids, true_exit=alone.get("true_exit"),
                         artifact=alone.get("artifact"))
                if alone.get("true_exit") == 0:
                    row = dict(row, alone_artifact=alone.get("artifact"),
                               unmeasured=f"order-dependent: {', '.join(ids)} failed in the whole gate and passed alone "
                                          f"({alone.get('artifact')})")
                    first = verdict.classify(row)
            return dict(row, verdict=first)
        # an environment fault is recovered, never fixed: its condition once, then one rerun;
        # room is the admission door the rerun re-enters, a database is a lease that answers
        j.append("gate-recovery", key=round_key, gate=g["name"], signature=first["signature"],
                 condition=first["recovery"])
        if leased and first["recovery"] == verdict.DB_ANSWERS:
            lease_into(genv)
        again = admitted(g, cmd, cwd, genv, holder)
        second = verdict.classify(again, after=first)
        j.append("verdict", key=round_key, gate=g["name"], attempt=2, rerun=True, recovered_from=first["signature"], **second)
        if second["valid"]:
            return dict(again, verdict=second, recovered_from=first["signature"])
        return dict(again, true_exit=None, red_ids=[], verdict=second, unmeasured=verdict.unmeasured_why(first, second))

    def read_remote(gates):
        rows = []
        for row in remote.measure(j, c, wt, slug, tip, gates):
            v = verdict.classify(row)
            if not v["valid"] and not row.get("unmeasured"):
                # an environment fault on the boxes is never rerun from here; it rides by its signature
                row = dict(row, true_exit=None, red_ids=[], unmeasured=f"{v['signature']} ({v['cause_class']}) on the CI box")
            j.append("verdict", key=round_key, gate=row["name"], attempt=1, **v)
            rows.append(dict(row, verdict=v))
        return rows

    def do():
        moved = [g for g in round_gates if g.get("remote")]
        down = None
        if moved:
            up, down = remote.available(c)
            if not up:
                j.append("remote-fallback", key=round_key, gates=[g["name"] for g in moved], why=down)
        here = [g for g in round_gates if not g.get("remote") or down]
        there = [g for g in moved if not down]
        with ThreadPoolExecutor(max_workers=max(1, parallel(c))) as pool:
            rows = dict(zip((g["name"] for g in here), pool.map(one, here)))
        rows.update((r["name"], r) for r in (read_remote(there) if there else []))
        return [dict(rows[g["name"]], fallback=f"{remote.FALLBACK}: {down}") if g.get("remote") and down
                else rows[g["name"]] for g in round_gates]
    # a gate that measured nothing is run again on a resume; a measured one never is
    measured = lambda rs: not any(r.get("true_exit") is None for r in rs or [])
    # a runner whose parent shell was killed can no longer run git or a gate; it stops, never fixes
    tip = gitops.head_of(wt)
    if not tip:
        raise RigFault(j, f"{round_key}: the tip of {wt} is unreadable, git did not answer")
    key = f"{round_key}@{tip[:9]}" + (f"+{stop_ids.short_hash(*answered)}" if answered else "")
    if answered and j.op_state(key)[0] != "complete":
        j.append("answer-routed", to="gate-rerun", key=key, answers=answered)
    results = j.op(j.fresh_key(key, measured), do)
    if lost_tools(results):
        raise RigFault(j, f"{round_key}@{tip[:9]}: none of its {len(results)} gates printed a TRUE_EXIT line")
    return results


def full_suite(j, wt, slug, base_sha, c, every_tip_only=False):
    """The heavy gates, keyed by tip alone: a second ask at a tip the suite already measured costs
    nothing. `every_tip_only` is the per-round lane (ruling 18): the gates the config marks
    `at: every-tip` run at every tip, the rest of the suite once when the loop is quiet and at the PR tip."""
    changed = gitops.changed_paths(wt, base_sha)
    if not every_tip_only:
        return measure(j, wt, slug, base_sha, changed, c, "gates-full", heavy=True)
    lane = [g for g in round_list(wt, changed, c) if g.get("at") == "every-tip"]
    return measure(j, wt, slug, base_sha, changed, c, "gates-tip", heavy=True, only=lane) if lane else []


def isolate_argv(template, ids):
    """A gate's `isolate` command with its red ids where it names `{id}`, each its own argument."""
    argv = []
    for token in _as_argv(template):
        argv += list(ids) if token == "{id}" else [token]
    return argv


def in_path_order(ids):
    """Test files or node ids sorted by their file path. pytest 9 binds a conftest's fixtures
    to the first directory node it collects, so a directory left and entered again on the
    command line loses them."""
    return sorted(ids, key=lambda i: (str(i).split("::", 1)[0].replace("\\", "/"), str(i)))

def replay_at_base(j, wt, base_sha, gate, slug):
    """The red tests of one gate, run once at the base commit in a detached worktree of
    the run's own tree, on their own leased database. The runner tells a red the change
    caused from one that was already there; nothing is stored, nothing goes stale."""
    ids = [i for i in (gate.get("red_ids") or []) if "::" in i]
    if not ids:
        return {"red_at_base": [], "measured": False, "why": "the gate named no test ids"}

    def do():
        # A fixed path collides with a copy a crash, a fixer or a failed remove left
        # behind, and the replay then measures nothing; each replay takes its own.
        tmp = Path(wt).parent / f"{Path(wt).name}-base-{int(time.time())}"
        gitops.git(wt, "worktree", "prune")
        rc, o, e = gitops.git(wt, "worktree", "add", "--detach", str(tmp), base_sha)
        if rc != 0:
            return {"red_at_base": [], "measured": False, "why": f"base worktree: {(o + e)[-200:]}"}
        try:
            cmd = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "auto", "--dist", "worksteal", *in_path_order(ids)]
            lease = lease_with_retry(tmp, slug + "-base", note=j.append)
            env = {GATE_NAME_ENV: gate["name"]}
            if lease.get("url"):
                env["DATABASE_URL"] = lease["url"]
            with admission.take("replay", f"{j.run_id} replay {gate['name']}", None, j.append):
                rc, js, text = hbcall.hb(["run-gate", "--issue", slug + "-base", "--", *cmd],
                                         cwd=tmp, timeout=policy.REPLAY_MIN * 60, env=env)
            got = read_true_exit(text)
            art = re.search(r"^GATE_OUTPUT=(.+)$", text, re.M)
            reds = [r.strip() for r in re.findall(r"^RED_NODEID=(.+)$", text, re.M)]
            return {"red_at_base": reds, "measured": got is not None, "true_exit": got,
                    "artifact": art.group(1).strip() if art else None, "base": base_sha}
        finally:
            test_db(tmp, "release", slug + "-base")
            gitops.git(wt, "worktree", "remove", "--force", str(tmp))
    # an unmeasured replay is asked again; caching it counted old reds against the change
    key = f"replay-base-{gate['name']}@{base_sha[:9]}"
    return j.op(j.fresh_key(key, lambda r: bool((r or {}).get("measured"))), do)


def settle_heavy(j, wt, base_sha, slug, gates, preexisting):
    """For every red heavy gate: a red id also red at base is pre-existing and journaled;
    the gate stays red only for the ids the change itself turned red."""
    out_pre = list(preexisting)
    for g in gates:
        # only a valid red is replayed: an environment fault at base proves nothing
        if not verdict.is_fixable(g) or not g.get("red_ids"):
            continue
        rep = replay_at_base(j, wt, base_sha, g, slug)
        if not rep.get("measured"):
            j.append("replay-at-base", gate=g["name"], measured=False, why=rep.get("why"))
            continue
        at_base = set(rep["red_at_base"])
        old = [i for i in g["red_ids"] if i in at_base]
        still = [i for i in g["red_ids"] if i not in old]
        j.append("replay-at-base", gate=g["name"], red_ids=g["red_ids"], red_at_base=old, still=still,
                 artifact=rep.get("artifact"))
        if old and not still:
            out_pre.append({"gate": g["name"], "evidence": rep.get("artifact"),
                            "why": f"every red id is red at base {base_sha[:9]} too", "red_ids": old})
            j.append("preexisting-red", gate=g["name"], evidence=rep.get("artifact"), red_ids=old)
        elif old:
            g["red_ids"] = still
            g["red_at_base"] = old
    return gates, out_pre


def test_db(wt, verb, slug):
    """The run's own test database through the venue's `database` capability, run in the
    worktree so the database is built from that tree: lease answers `{url}`, release
    `{dropped}`, a refusal `{why}`; a venue without the capability `{why: not offered}`."""
    if not adapter.offers("database"):
        return {"why": adapter.not_offered("database")}
    rc, js, text = adapter.call(f"database_{verb}", cwd=wt, timeout=policy.TEST_DB_MIN * 60, slug=slug)
    if rc != 0:
        return {"why": js["why"] if isinstance(js, dict) and js.get("why") else text[-400:]}
    if verb == "lease":
        return {"url": js["url"]} if isinstance(js, dict) and js.get("url") else {"why": f"the lease printed no url: {text[-300:]}"}
    return {"dropped": True}


def lease_with_retry(wt, slug, waits=(15, 30, 60, 120, 240), note=None):
    """A lease may clone one shared source the database refuses to clone while another
    session touches it, so every lease on the host takes the lease slot first."""
    def lease_once():
        with machine.slot("database-lease", f"lease {slug}", note=note):
            return test_db(wt, "lease", slug)
    lease = lease_once()
    for wait in waits:
        # a refusal a product signature names as a lease's to answer is a busy venue, not a red
        if lease.get("url") or not verdict.lease_busy(lease.get("why") or lease):
            break
        time.sleep(wait)
        lease = lease_once()
    return lease


def gates_red(results, preexisting=()):
    """Every gate the PR must name: not WORKS by its verdict, not proven red at base, and
    not one only CI's runner can run or one with nothing selected to measure."""
    names = {p.get("gate") for p in preexisting}
    return [r for r in results if r.get("name") not in names and verdict.is_red(r)]


def gates_to_fix(results, preexisting=()):
    """The reds a fixer can turn green: valid and the product's own, by their verdict."""
    return [r for r in gates_red(results, preexisting) if verdict.is_fixable(r)]


def known_reds(j):
    """Every gate this run proved red before the change, from the journal: the one record
    any gate check may consult, whichever stage did the proving."""
    seen = {}
    for e in j.events():
        if e.get("kind") == "preexisting-red" and e.get("gate"):
            seen[e["gate"]] = {"gate": e["gate"], "evidence": e.get("evidence"), "red_ids": e.get("red_ids") or []}
    return list(seen.values())


def unmeasured_gate_names(gates):
    """Gates whose verdict is UNMEASURED and make a landing red; a CI step only CI's runner
    can run is listed in the PR, never counted here."""
    return [g["name"] for g in gates or [] if verdict.is_red(g) and verdict.classify(g)["outcome"] == verdict.UNMEASURED]


def pr_tip(j, wt, slug, base_sha, c, gates, stops):
    """Every instrument CI will run, measured at the tip the PR is about to carry: CI's own
    scripts (ciparity), the product config's heavy gates (the boxes') and its every-tip gates.
    Each is keyed by the tip, so a tip already measured costs nothing and a tip a late fix moved
    is measured again. A red CI script stops the PR with the gate named, unless the run already
    proved it red before the change (known_reds); a red heavy gate rides the PR by name. One CI
    alone can run is listed UNMEASURED. Journals `pr-tip` with the tip and the heavy rows, which
    the lander reads before it opens a PR. Returns the run's gates with every row this measured
    replacing the one of the same name."""
    tip = gitops.head_of(wt)
    changed = gitops.changed_paths(wt, base_sha)
    parity = ci_parity(wt, c)
    ran = measure(j, wt, slug, base_sha, changed, c, "gates-ci-parity", only=parity["gates"]) if parity["gates"] else []
    heavy = full_suite(j, wt, slug, base_sha, c)
    known = known_reds(j)
    heavy, _pre = settle_heavy(j, wt, base_sha, slug, heavy, known)
    stop_ids.gates(j, stops, gates_red(ran, known), "a CI-parity gate is red on the tip the PR carries")
    j.append("ci-parity", ran=[{"name": g["name"], "true_exit": g.get("true_exit")} for g in ran],
             known_red=[g["name"] for g in gates_red(ran) if g not in gates_red(ran, known)],
             unmeasured=[{"name": g["name"], "why": g["unmeasured"]} for g in parity["unmeasured"]])
    j.append("pr-tip", tip=tip, heavy=[{"name": g["name"], "true_exit": g.get("true_exit"),
                                        "unmeasured": g.get("unmeasured")} for g in heavy],
             parity=[g["name"] for g in ran])
    fresh = {g["name"]: g for g in list(ran) + list(heavy) + list(parity["unmeasured"])}
    return [g for g in gates or [] if g.get("name") not in fresh and not g.get("ci_parity")] + list(fresh.values())


def heavy_at_tip(j, tip):
    """The heavy rows that measured something at `tip` (its `pr-tip`), or [] when none did: an
    UNMEASURED row is no measurement, and a PR opened there would carry a tip no heavy
    instrument read."""
    rows = []
    for e in j.events():
        if e.get("kind") == "pr-tip" and e.get("tip") == tip:
            rows = [r for r in e.get("heavy") or [] if r.get("true_exit") is not None]
    return rows


