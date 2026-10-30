"""hb.ci - watching CI and deploys."""

import json
import time
import urllib.request

from hb.rig.common import branch_setting, cfg, gh, out, _probe_outage
from hb.venue import adapter


def cmd_ci_watch(a):
    """WAIT for CI, do not glance at it: a one-shot probe returns in a second with nothing
    waited on, and its exit code reads like a measurement of a run that had not started.

    Polls every 60s until every check concludes or --timeout-min elapses.
    Exit 0 = all green, 2 = something red (named), 10 = TIMEOUT ONLY (pending named).
    """
    c = cfg()
    deadline = time.time() + (a.timeout_min or 45) * 60
    poll_s = 60
    polls = 0
    states, pending, bad = {}, [], []
    last_err = None
    while True:
        polls += 1
        rc, o, e = gh(["pr", "checks", str(a.pr), "--repo", c["repo"],
                       "--json", "name,state", "--jq", "."], timeout=60)
        if rc != 0:
            probe = _probe_outage(c)
            if not probe.get("api_reachable"):
                out({"verdict": "external-outage", "polls": polls,
                     "detail": "provider unreachable, not a CI failure - pause_class "
                               "external-outage, wait-and-retry", "probe": probe}, 2)
            # A transient gh error is not an answer about CI. Keep waiting; the
            # error is carried so a timeout can say what it kept hitting.
            last_err = e.strip()[:400]
        else:
            last_err = None
            checks = json.loads(o) if o else []
            states = {ch["name"]: ch["state"] for ch in checks}
            bad = [n for n, st in states.items()
                   if st in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT")]
            pending = [n for n, st in states.items()
                       if st in ("PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "")]
            if bad:
                out({"verdict": "ci-failed", "failed": bad, "all": states,
                     "polls": polls}, 2)
            if states and not pending:
                out({"verdict": "green", "checks": states, "polls": polls}, 0)
            # Zero checks is "never measured", not "not yet" - but it is also the
            # normal state of a PR opened seconds ago, so it is WAITED on like any
            # other unsettled state and only becomes a verdict at the deadline.
        remaining = deadline - time.time()
        if remaining <= 0:
            if not states:
                out({"verdict": "no-checks-registered", "polls": polls,
                     "timeout_min": a.timeout_min or 45,
                     "detail": "PR reported ZERO checks for the whole watch - verify "
                               "the PR number and that required workflows trigger "
                               "for this base branch",
                     "last_gh_error": last_err}, 10)
            out({"verdict": "timeout", "pending": pending, "all": states,
                 "polls": polls, "timeout_min": a.timeout_min or 45,
                 "settled": len(states) - len(pending),
                 "detail": "the watch hit its own bound with checks still running - "
                           "this is UNMEASURED, not a pass and not a failure",
                 "last_gh_error": last_err}, 10)
        time.sleep(min(poll_s, max(1, remaining)))


def _served_sha(url):
    """(sha, why): what the deployed tier says it serves at `deploy.version_url`."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace")[:2000])
        sha = str(data.get("sha") or data.get("commit") or data.get("version") or "")
        return (sha, None) if sha else (None, "the version url named no sha")
    except Exception as ex:  # noqa: BLE001 - any failure is UNMEASURED, named
        return None, f"{ex.__class__.__name__}: {str(ex)[:160]}"


def cmd_deploy_watch(a):
    """One probe of the product's deploy (`deploy.workflow`), then of what the deployed tier
    serves (`deploy.version_url`). Exit 0 settled-green and serving the sha, 2 settled-bad or
    not offered, 10 keep waiting; the caller's loop owns the waiting."""
    c = cfg()
    if not adapter.offers("deploy"):
        out({"verdict": adapter.not_offered("deploy")}, 2)
    deploy = c.get("deploy") or {}
    if not deploy.get("workflow"):
        out({"verdict": "refused", "error": "harness-config.json deploy.workflow is not set"}, 2)
    # --sha (the merge SHA) pins WHICH deploy run is watched: the latest run on the branch
    # can belong to a different concurrent merge. The run may not exist yet right after the
    # merge, so the caller polls for it to appear.
    want = (a.sha or "").strip()
    # --branch names a ref a deploy was dispatched on (an integration branch); the sha
    # still pins the run.
    _branch = (getattr(a, "branch", None) or "").strip() or branch_setting(c, "landing")
    rc, o, e = gh(["run", "list", "--repo", c["repo"], "--branch", _branch,
                   "--workflow", deploy["workflow"], "-L", "10",
                   "--json", "databaseId,status,conclusion,headSha"])
    if rc != 0:
        probe = _probe_outage(c)
        if not probe.get("api_reachable"):
            out({"verdict": "external-outage", "external": probe}, 2)
        out({"verdict": "gh-error", "detail": e.strip()[:400]}, 10)
    runs = json.loads(o)
    if want:
        match = [r for r in runs if r.get("headSha", "").startswith(want)
                 or want.startswith(r.get("headSha", "")[:12])]
        if not match:
            # merge landed but its run has not appeared yet; a caller that has probed
            # >5min without a run appearing should verify the merge SHA / workflow name
            out({"verdict": "no-run-yet", "sha": want[:12]}, 10)
        run = match[0]
    else:
        if not runs:
            out({"verdict": "no-run-yet", "detail": "no deploy run found on the branch"}, 10)
        run = runs[0]
    run_id = run["databaseId"]
    rc, o, _ = gh(["run", "view", str(run_id), "--repo", c["repo"], "--json", "status,conclusion"])
    if rc != 0:
        out({"verdict": "gh-error", "run_id": run_id}, 10)
    r = json.loads(o)
    if r.get("status") != "completed":
        out({"verdict": "running", "run_id": run_id, "status": r.get("status")}, 10)
    if r.get("conclusion") != "success":
        out({"verdict": "deploy-failed", "run_id": run_id, "conclusion": r.get("conclusion")}, 2)
    # A green run is a claim that something deployed; the version url is what is served.
    if not deploy.get("version_url"):
        out({"verdict": "green", "run_id": run_id, "conclusion": r.get("conclusion"),
             "served": "UNMEASURED: deploy.version_url is not set"}, 0)
    served, why = _served_sha(deploy["version_url"])
    if served is None:
        out({"verdict": "serving-unmeasured", "run_id": run_id, "why": why}, 10)
    if want and not (served.startswith(want[:7]) or want.startswith(served[:7])):
        out({"verdict": "not-serving-yet", "run_id": run_id, "served": served, "sha": want[:12]}, 10)
    out({"verdict": "green", "run_id": run_id, "conclusion": r.get("conclusion"), "served": served}, 0)
