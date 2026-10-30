"""hb.run.closeout - a run is done when it is merged and nothing of it is left.

    hb closeout <run id>

After the owner merges: confirm the merge, sweep what the run left (hb tidy) and purge the
dev venues it made (hb.devshops), check each thing is really gone, see that the deployed tier
serves the merge (under the venue's `deploy` capability, from `deploy.version_url`), and name
every red row the merge carried; a run files no issues, the session that reads the PR decides
what is owed. The issues the PR names (`Issue: #N`) are closed here, after the merge, never by
a PR trailer. A run
with a screen item owes a demo, and the closeout starts it (hb.run.demo). What it found is written to the
run's evidence as closeout.json, which the run page shows. It reports clean only when
every check says so; a thing it could not remove is named, never dropped.
"""
import json
import subprocess
import sys

from hb.rig.common import HB_ENTRY, canon, cfg, gh, out, state_dir
from hb.venue import devshops
from hb.beats import demo
from hb.rig import gitops
from hb.record import prune
from hb.record import success
from hb.record import view
from hb.record.journal import Journal


def _hb(*argv, timeout=1800):
    r = subprocess.run([sys.executable, HB_ENTRY, *argv], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, cwd=str(canon()))
    for line in reversed((r.stdout or "").splitlines()):
        if line.strip().startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def close_issues(run_id, pr):
    """Close each issue the PR names by `Issue: #N`, read off the body the lander wrote, now
    that the PR merged (a `Closes` trailer raced the review gate). Returns
    [{issue, closed, why}]."""
    import re
    body = view.root() / run_id / "evidence" / "pr-body.md"
    text = body.read_text(encoding="utf-8") if body.exists() else ""
    out_ = []
    for n in re.findall(r"^Issue: #(\d+)\s*$", text, re.M):
        rc, o, e = gh(["issue", "close", n, "--comment", f"Merged in {pr}."], timeout=60)
        out_.append({"issue": n, "closed": rc == 0, "why": None if rc == 0 else (e or o or "").strip()[:160]})
    return out_


def _merged(pr):
    rc, o, _ = gh(["pr", "view", str(pr), "--json", "state,mergedAt,mergeCommit,mergedBy,headRefName"])
    try:
        return json.loads(o or "{}") if rc == 0 else {}
    except ValueError:
        return {}


# whether the deployed tier serves the merge: the one reader, shared with the demo's deploy wait
_live = demo.deployed


def paid_runner_jobs(rows):
    """The jobs that ran on GitHub's own machines, from (job, runner group) rows. The owner's
    runners are their own; a job on GitHub's is the one thing in a run that is billed."""
    return sorted({str(name) for name, group in rows if str(group or "").strip().lower() == "github actions"})


def _paid_runners(branch):
    """(jobs on GitHub's machines, why it could not be read): the PR branch's latest CI runs."""
    rc, o, e = gh(["run", "list", "--branch", branch, "--limit", "6", "--json", "databaseId"])
    try:
        ids = [r["databaseId"] for r in json.loads(o or "[]")] if rc == 0 else None
    except (ValueError, KeyError, TypeError):
        ids = None
    if ids is None:
        return [], f"the CI runs could not be listed ({(e or '').strip()[:80]})"
    rows = []
    for rid in ids:
        rcj, oj, _ = gh(["api", f"repos/{{owner}}/{{repo}}/actions/runs/{rid}/jobs", "--jq",
                         ".jobs[] | [.name, (.runner_group_name // \"\")] | @tsv"])
        if rcj == 0:
            rows += [tuple((line.split("\t") + [""])[:2]) for line in (oj or "").splitlines() if line.strip()]
    return paid_runner_jobs(rows), None


def _reds_carried(v):
    """The red rows the merge carried, named for the report. A run files no issue for them
    (the owner's ruling): the PR states them, and the session that reads it decides the work."""
    reds = [r for r in v.get("items") or [] if r.get("state") in ("BROKEN", "DIVERGED", "UNMEASURED") and r.get("substate") != "by-test"]
    return [f"{r['item']} {('is broken' if r['state'] != 'UNMEASURED' else 'was not measured')}: {str(r.get('text') or '')[:80]}" for r in reds]


def owe_demo(run_id, v, pr, sha, no_demo=False, start=None):
    """After every merge a run owes a demo: `evidence/demo-owed.json` names the run, the PR,
    the merge commit and its issue or wave, and the demo starts itself (`hb demo-launch`,
    apart from this process) unless --no-demo. Returns what the report says."""
    owed = {"run_id": run_id, "pr": pr, "merge_sha": sha}
    if v.get("kind") == "wave":
        owed["wave"] = v.get("ref")
    else:
        owed["issue"] = v.get("issue")
    path = demo.owed_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(owed, indent=2), encoding="utf-8")
    if no_demo:
        return {"owed": True, "path": str(path), "launched": None, "why": "--no-demo"}
    return {"owed": True, "path": str(path), "launched": (start or demo.start_detached)(run_id)}


def purge_dev_venues(run_id, purge=None):
    """The cleaned rows for the dev venues the registry says this run made, each purged through
    the product's door; a venue the product refused is left behind with its words."""
    path = devshops.registry_path()
    rows = devshops.purge_all(path, devshops.owned_by(devshops.read(path), f"run:{run_id}"), purge=purge)
    return [{"what": f"Dev venues this run made: {r['name']}", "ok": r["ok"],
             "state": None if r["ok"] else f"left behind: {r['words']}"} for r in rows]


def success_check(run_id, merged_at):
    """The run's Success block with each entry's due date from the merge, journaled once as
    `success-check` so the watch and the closeout surface it; the check itself is a later
    command. Nothing is written until the PR reports a merge time that reads."""
    j = Journal(run_id)
    entries = j.fact("success")
    if not entries or success.merged_time(merged_at) is None:
        return []
    if not any(e.get("kind") == "success-check" for e in j.events()):
        for e in success.due(entries, merged_at):
            j.append("success-check", due=e.get("due"), **{k: e.get(k) for k in success.KEYS})
    return [e for e in j.events() if e.get("kind") == "success-check"]


def cmd_closeout(a):
    c = cfg()
    v = view.run(a.run_id)
    if not v:
        out({"verdict": "no-such-run", "run_id": a.run_id}, 2)
    pr = a.pr or v.get("pr")
    m = _merged(pr) if pr else {}
    if m.get("state") != "MERGED":
        out({"verdict": "not-merged", "run_id": a.run_id, "pr": pr, "state": m.get("state") or "unknown",
             "why": "a closeout runs after the owner merges"}, 2)
    tidy = _hb("tidy", "--apply")
    root = canon()
    branch = m.get("headRefName") or ""
    cleaned = []
    wts = gitops.git(root, "worktree", "list", "--porcelain")[1]
    cleaned.append({"what": "Working copy", "ok": f"refs/heads/{branch}" not in wts, "state": None})
    local = gitops.git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")[0] != 0
    remote = not gitops.git(root, "ls-remote", "--heads", "origin", branch, timeout=120)[1].strip()
    cleaned.append({"what": "Branch, local and remote", "ok": local and remote,
                    "state": None if local and remote else ("still on the remote" if local else "still here")})
    venues = [x for x in (_hb("venue", "list").get("venues") or []) if x.get("state") == "up"]
    cleaned.append({"what": "Stand-ins and their test logins", "ok": not venues,
                    "state": None if not venues else f"{len(venues)} still up"})
    cleaned += purge_dev_venues(a.run_id)
    for line in tidy.get("left_behind") or []:
        cleaned.append({"what": str(line)[:160], "ok": False, "state": "left behind"})
    for row in cleaned:
        row["state"] = row["state"] or ("gone" if row["ok"] else "left behind")
    sha = ((m.get("mergeCommit") or {}).get("oid")) or ""
    live_ok, live = _live(c, sha)
    reds = _reds_carried(v)
    clean = all(x["ok"] for x in cleaned)
    paid, paid_why = _paid_runners(branch) if branch else ([], "no branch")
    # the records of the sitting that filed this run exist on one disk until this pushes them
    # sync answers no-repo on its own when the records have no checkout and no mirror
    backup = _hb("sittings", "sync")
    demo_owed = owe_demo(a.run_id, v, pr, sha, no_demo=a.no_demo)
    checks = success_check(a.run_id, m.get("mergedAt"))
    issues = close_issues(a.run_id, pr)
    report = {"run_id": a.run_id, "pr": pr, "merged": f"{(m.get('mergedBy') or {}).get('login') or 'someone'}, {m.get('mergedAt')}",
              "merged_at": m.get("mergedAt"), "merge_sha": sha,
              "live": live, "live_ok": live_ok, "cleaned": cleaned, "reds_carried": reds, "clean": clean,
              "paid_runners": {"jobs": paid, "unread": paid_why}, "demo": demo_owed, "success_check": checks,
              "issues_closed": issues,
              "sittings_sync": backup.get("verdict") or "no answer",
              "summary": ("Merged, and nothing left behind." if clean else "Merged. Something was left behind, named below.")
                         + ("" if live_ok else f" Live check: {live}.")}
    # merged is the last reader of the films and streams; the demo it starts keeps its folder
    report["pruned"] = prune.prune_run(view.root() / a.run_id, keep_dirs=("demo",))
    if not demo_owed.get("owed"):
        report["collapsed"] = prune.collapse(view.root() / a.run_id, state_dir() / "evidence")
    (view.root() / a.run_id / "evidence").mkdir(parents=True, exist_ok=True)
    (view.root() / a.run_id / "evidence" / "closeout.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    out({"verdict": "closed" if clean else "left-behind", **report}, 0 if clean else 1)


def register(sub):
    s = sub.add_parser("closeout", help="after the merge: sweep what a run left, check it is gone and live, name the red rows it carried")
    s.add_argument("run_id")
    s.add_argument("--pr", help="the PR, when the run did not open it itself")
    s.add_argument("--no-demo", dest="no_demo", action="store_true",
                   help="write demo-owed.json but do not start the demo after the deploy")
