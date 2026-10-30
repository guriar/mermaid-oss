"""hb.run.demo - the demo starts itself after every merge.

    hb demo-launch <run id> [--wait-min N]

A closeout that finds a screen item in the merged run writes `evidence/demo-owed.json` and
starts this verb apart from itself. Under the venue's `deploy` capability it waits until the
product's `deploy.version_url` serves the merge commit, then sits one seat (config `seats.demo`,
through the same adapters as every seat) whose brief is `commands/demo.md` and the run's facts,
in the product checkout, writing under the run's `evidence/demo/`. The venue it films is
provisioned through the venue's `demo_provision` under `accounts`. What it did is written to
`evidence/demo/launch.json`, which the session that reads the closeout relays; a capability
the venue lacks is named there as not offered.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from hb.rig.common import HB_ENTRY, canon, cfg, now_iso, out, report_text
from hb.rig import gitops
from hb.venue import adapter
from hb.venue import devshops
from hb.runner import adapters
from hb.contract import policy
from hb.record import view

DEPLOY_WAIT_MIN = 90          # a deploy measured 4 to 12 minutes; the fuse, never the cut
DEPLOY_POLL_S = 30


def owed_path(run_id):
    return view.root() / run_id / "evidence" / "demo-owed.json"


def deployed(c, sha):
    """(True, False or None, words): whether the deployed tier serves `sha`, from the version answer
    at config `deploy.version_url` (its `sha`). None when nothing could be measured: no `deploy`
    capability, no address, or no answer. A later merge may be what is served: the merge is live
    when the served commit holds it."""
    if not adapter.offers("deploy", c):
        return None, adapter.not_offered("deploy")
    url = ((c.get("deploy") or {}).get("version_url") or "").strip()
    if not url or not sha:
        return None, "not checked: no deploy.version_url or merge commit"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            served = str((json.loads(r.read().decode("utf-8") or "{}") or {}).get("sha") or "")
    except Exception as ex:  # noqa: BLE001 - any failure to read is an unmeasured answer, named
        return None, f"not checked: the version address did not answer ({ex.__class__.__name__})"
    gitops.git(canon(), "fetch", "-q", "origin", timeout=300)
    if served and gitops.git(canon(), "merge-base", "--is-ancestor", sha, served)[0] == 0:
        return True, f"{url} serves the merge"
    return False, f"{url} serves {served[:9] or 'an unknown commit'}, which does not hold the merge yet"


def deploy_probe(sha):
    """One read of the deployed tier: (exit code, its answer). 0 serves the merge, 10 keep waiting,
    2 nothing can be measured."""
    live, words = deployed(cfg(), sha)
    return (0 if live else 10 if live is False else 2), {"live": live, "words": words}


def wait_for_deploy(sha, probe=None, poll_s=DEPLOY_POLL_S, wait_min=DEPLOY_WAIT_MIN, sleep=time.sleep):
    """(green, the last answer): the deploy of `sha` is polled until it settles or the fuse."""
    probe = probe or deploy_probe
    t0 = time.time()
    while True:
        rc, ans = probe(sha)
        if rc == 0:
            return True, ans
        if rc == 2 or time.time() - t0 > wait_min * 60:
            return False, dict(ans or {}, rc=rc)
        sleep(poll_s)


def demo_text():
    """commands/demo.md without its front matter: the harness repo's own, else the product's."""
    here = Path(HB_ENTRY).resolve().parent.parent / "commands" / "demo.md"
    path = here if here.exists() else canon() / ((cfg().get("commands_dir") or ".claude/commands")) / "demo.md"
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[-1]
    return text.strip()


def run_provisioner(slug):
    """The venue's `demo_provision` for `slug`: its receipt, or {"error": ...} when it gave none."""
    rc, js, text = adapter.call("demo_provision", cwd=canon(), timeout=1800, slug=slug)
    return js if rc == 0 and isinstance(js, dict) else {"error": text[-300:]}


def provision_venue(run_id, provision=None):
    """(the owned registry line or None, the provisioner's receipt). The launcher provisions the
    venue itself and stamps the run as owner the moment the tenant exists, so a seat that dies
    after leaves a venue the closeout and the sweep can find. The registry name is the slug."""
    slug = f"demo-{run_id}".lower()
    receipt = (provision or run_provisioner)(slug)
    if not receipt.get("tenant_id"):
        return None, receipt
    name = receipt.get("name") or slug
    line = devshops.register(devshops.registry_path(), name, f"run:{run_id}", "demo",
                             devshops.DEMO_TTL_DAYS, receipt["tenant_id"])
    return dict(line, tenant_id=receipt["tenant_id"]), receipt


def brief(owed, out_dir, venue):
    evidence = view.root() / owed["run_id"] / "evidence"
    facts = ["", "# This demo", "",
             f"- The run: {owed['run_id']}, merged as {owed.get('merge_sha')} through {owed.get('pr')}.",
             f"- What changed: the run's record `{evidence / 'record.md'}` (every item, its row and why, the "
             f"builder's decisions) and the PR body beside it, and the merge commit's CHANGELOG section.",
             "- The deployed tier serves the merge commit; film the deployed tier.",
             f"- Your venue is `{venue['name']}` (tenant {venue.get('tenant_id')}), already provisioned and owned by this run.",
             f"- Write `plan.md`, the video and `chapters.md` under `{out_dir}`, and last a `summary.md` there: one "
             "paragraph of what you showed and the chapters labelled red or not shown. No conductor spawned you: "
             "the session that reads the closeout sends the owner the video and the chapters."]
    return demo_text() + "\n" + "\n".join(facts) + "\n"


def launch_demo(run_id, probe=None, poll_s=DEPLOY_POLL_S, wait_min=DEPLOY_WAIT_MIN, sleep=time.sleep, runtime=None,
                provision=None):
    """Wait for the deploy, provision the run's venue, then sit the demo seat. Returns what launch.json records."""
    try:
        owed = json.loads(report_text(owed_path(run_id)))
    except (OSError, ValueError):
        return {"verdict": "nothing-owed", "run_id": run_id, "why": "no evidence/demo-owed.json: the closeout owed no demo"}
    out_dir = view.root() / run_id / "evidence" / "demo"
    out_dir.mkdir(parents=True, exist_ok=True)

    def record(rec):
        rec = dict(rec, run_id=run_id, ts=now_iso())
        (out_dir / "launch.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        return rec
    # the default probe reads the deploy and the default provisioner the accounts; an injected one asks neither
    for capability, injected in (("deploy", probe), ("accounts", provision)):
        if injected is None and not adapter.offers(capability):
            return record({"verdict": "no-demo", "why": adapter.not_offered(capability)})
    record({"verdict": "waiting-for-deploy", "sha": owed.get("merge_sha")})
    green, ans = wait_for_deploy(owed.get("merge_sha") or "", probe=probe, poll_s=poll_s, wait_min=wait_min, sleep=sleep)
    if not green:
        return record({"verdict": "no-demo", "why": "the deployed tier does not serve the merge", "deploy": ans})
    name, model = adapters.seat_runtime("demo", runtime)
    ok, why = adapters.probe(name, "demo")
    if not ok:
        return record({"verdict": "no-demo", "why": why, "deploy": ans})
    venue, receipt = provision_venue(run_id, provision)
    if not venue:
        return record({"verdict": "no-demo", "why": "the venue did not provision", "deploy": ans,
                       "provision": {k: receipt.get(k) for k in ("error", "failed_step")}})
    venue_rec = {k: venue.get(k) for k in ("name", "tenant_id", "owner", "expires")}
    record({"verdict": "filming", "deploy": ans, "venue": venue_rec})
    (out_dir / "brief.md").write_text(brief(owed, out_dir, venue), encoding="utf-8")
    env = {"HB_OUTPUT_PATH": str(out_dir / "report.json"), "HB_RUN_ID": run_id, "HB_ISSUE": str(owed.get("issue") or "")}
    res = adapters.launch(name, "demo", out_dir / "brief.md", canon(), out_dir, model=model,
                          timeout_s=policy.SEAT_FUSE_MIN * 60, env=env, effort=adapters.seat_effort("demo"))
    return record({"verdict": "filmed" if res.get("exit_class") == "ok" else res.get("exit_class"), "deploy": ans,
                   "venue": venue_rec,
                   "runtime": name, "model": model, "seconds": res.get("seconds"), "stdout": res.get("stdout"),
                   "usage_limit": res.get("usage_limit"), "out_dir": str(out_dir)})


def start_detached(run_id):
    """`hb demo-launch <run id>` as its own process, so a closeout never waits on a deploy."""
    from hb.runner.detach import child_env
    log = view.root() / run_id / "evidence" / "demo-launch.out"
    log.parent.mkdir(parents=True, exist_ok=True)
    kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt"
          else {"start_new_session": True})
    with open(log, "ab") as f:
        p = subprocess.Popen([sys.executable, HB_ENTRY, "demo-launch", run_id], cwd=str(canon()), env=child_env(os.environ),
                             stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, close_fds=True, **kw)
    return {"pid": p.pid, "log": str(log)}


def cmd_demo_launch(a):
    rec = launch_demo(a.run_id, wait_min=a.wait_min)
    # the demo is the last reader of the run's evidence and of its worktree's transcripts
    from hb.record import prune
    from hb.rig.common import state_dir
    rec["collapsed"] = prune.collapse(view.root() / a.run_id, state_dir() / "evidence")
    out(rec, 0 if rec.get("verdict") == "filmed" else 2)


def register(sub):
    s = sub.add_parser("demo-launch", help="after a merge: wait for its deploy, then film it with the demo seat")
    s.add_argument("run_id")
    s.add_argument("--wait-min", dest="wait_min", type=int, default=DEPLOY_WAIT_MIN,
                   help="the fuse on waiting for the deploy of the merge commit")
