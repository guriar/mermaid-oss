"""hb.beats.here - the instruments as verbs a session composes without a contract (ruling 17).

A plain session asked to reverse engineer something and fix it keeps its understanding; it does
not hand a sentence to a fresh builder. It opens a worktree, works there, asks for a review of
its diff against its own ask, runs the gates, and lands. Each verb is one instrument with one
receipt, journaled under one run per worktree so a second call after a crash is idempotent, and
the beats call the same functions (loop.review_once, gitops.worktree, landing.land), so there is
one decider per instrument.

    hb worktree open <slug>            a worktree off branches.landing, branch here/<slug>
    hb review --here "<ask>"           one reviewer seat on this worktree's diff against the ask
    hb audit --here                    the venue up and one audit seat driving the change (ui)
    hb land                            the record and the PR from the receipts collected
"""
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

from hb.beats import loop
from hb.beats.hotfix import DEFAULT_SEAT_TIMEOUT_MIN
from hb.record.journal import Journal
from hb.rig import gitops
from hb.venue import adapter
from hb.rig.common import canon, cfg, invoking_root, out, state_dir

BRANCH_PREFIX = "here/"
DIR_PREFIX = "here-"
NO_CONTRACT = "no contract supplied; completeness not measured"


def run_id_for(slug):
    """One run per worktree slug, so every verb journals into the same place and resumes its op keys."""
    return f"here-{slug}"


def _slug_of(worktree):
    """The slug a here/ worktree was opened under, from its branch; None when this is not one."""
    rc, branch, _ = gitops.git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    branch = (branch or "").strip()
    return branch[len(BRANCH_PREFIX):] if rc == 0 and branch.startswith(BRANCH_PREFIX) else None


def _here():
    """(worktree, slug, journal) for the worktree the caller stands in, or a refusal."""
    root = invoking_root()
    if not root:
        out({"verdict": "refused", "why": "not inside a git worktree"}, 2)
    slug = _slug_of(root)
    if not slug:
        out({"verdict": "refused", "why": f"{root} is not a worktree hb opened (its branch does not start with {BRANCH_PREFIX}); "
                                          f"run `hb worktree open <slug>` first"}, 2)
    return Path(root), slug, Journal(run_id_for(slug))


def _evidence(j):
    d = state_dir() / "runs" / j.run_id / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_worktree_open(a):
    slug = re.sub(r"[^a-z0-9-]+", "-", a.slug.lower()).strip("-")
    if not slug:
        out({"verdict": "refused", "why": "the slug is empty after slugifying"}, 2)
    c = cfg()
    j = Journal(run_id_for(slug))
    if not j.fact("harness"):
        j.open_run("here", slug=slug)
    reused = bool(j.fact("worktree"))  # the op is journaled complete: a second open returns the same tree
    # a refused open is a failed effect: the next open tries again instead of handing back the refusal
    key = j.fresh_key("worktree", lambda r: isinstance(r, dict) and not r.get("error"))
    tree = gitops.worktree(j, slug, canon(), c, shadow=False, branch_prefix=BRANCH_PREFIX, dir_prefix=DIR_PREFIX,
                           base_ref=getattr(a, "base", None), key=key, prepare=adapter.prepare_worktree)
    if tree.get("error"):
        out({"verdict": "refused", "why": tree["error"], "run_id": j.run_id}, 2)
    j.phase("worktree", worktree=tree["worktree"], branch=tree["branch"], base_sha=tree["base_sha"])
    out({"verdict": "open", "run_id": j.run_id, "worktree": tree["worktree"], "branch": tree["branch"],
         "base_sha": tree["base_sha"], "reused": reused or bool(tree.get("reused")),
         "next": f"work in {tree['worktree']}; then `hb review --here \"<your ask>\"`, `hb run-gate`, `hb land`"}, 0)


def cmd_review_here(a):
    wt, slug, j = _here()
    base_sha = j.fact("base_sha") or ""
    if not base_sha:
        out({"verdict": "refused", "why": "this worktree's run records no base_sha; open it with `hb worktree open`"}, 2)
    tip = gitops.head_of(wt)
    rc, dirty, _ = gitops.git(wt, "status", "--porcelain")
    # the reviewer reads the ask as the goal and one item; the standards floor comes with the role page
    contract = {"issue": slug, "goal": a.ask, "body": a.ask,
                "items": [{"item": "ASK", "kind": "code", "text": a.ask, "then": a.ask}]}
    evidence = _evidence(j)
    seat_kw = dict(runtime=a.runtime, timeout_min=a.seat_timeout_min, shadow=True, evidence=evidence,
                   rate_limit_wait_min=a.rate_limit_wait_min)
    extra = {"What you are reviewing": f"the diff {base_sha[:9]}..{tip[:9]} in your working directory"
                                       + (", plus uncommitted changes" if (dirty or "").strip() else ""),
             "No contract": NO_CONTRACT + "; grade the ask and the standards floor only"}
    stops = []
    review, fix = loop.review_once(j, contract, {}, wt, base_sha, seat_kw, extra, stops)
    report = (review.get("report") or {}) if review.get("result") == "ok" else {}
    # the ask becomes a fact of the run, so audit and land default to it
    j.phase("review-here", ask=a.ask, tip=tip)
    receipt = {"verdict": review.get("result"), "run_id": j.run_id, "ask": a.ask, "base_sha": base_sha, "tip": tip,
               "uncommitted": bool((dirty or "").strip()), "contract": NO_CONTRACT,
               "findings": report.get("findings") or [], "to_fix": [f.get("id") for f in fix],
               "goal_verdict": report.get("goal_verdict"), "stops": stops, "seat": review.get("seat")}
    (evidence / "review-here.json").write_text(json.dumps(receipt, indent=1, ensure_ascii=False), encoding="utf-8")
    out(receipt, 0 if review.get("result") == "ok" else 2)


def _ask_contract(slug, ask):
    """The one-item contract a session's ask stands for: the ask is the goal and the item."""
    return {"issue": slug, "goal": ask, "body": ask, "items": [{"item": "ASK", "kind": "code", "text": ask, "then": ask}]}


def _seat_kw(a, evidence):
    return dict(runtime=a.runtime, timeout_min=a.seat_timeout_min, shadow=True, evidence=evidence,
                rate_limit_wait_min=a.rate_limit_wait_min)


def cmd_audit_here(a):
    """The venue up through the venue block and one audit seat driving the change; rows in the receipt."""
    from hb.beats import drive
    from hb.venue import adapter, standin
    wt, slug, j = _here()
    base_sha = j.fact("base_sha") or ""
    evidence = _evidence(j)
    ask = a.ask or j.fact("ask") or "the change in this worktree"
    if not adapter.offers("ui"):
        receipt = {"verdict": "not-offered", "run_id": j.run_id, "why": adapter.not_offered("ui"), "rows": []}
        (evidence / "audit-here.json").write_text(json.dumps(receipt, indent=1), encoding="utf-8")
        out(receipt, 0)
    contract = _ask_contract(slug, ask)
    v = standin.venue_up(j, slug, wt, real_llm=False, key=f"venue-up-audit-{gitops.head_of(wt)[:9]}")
    if not (v or {}).get("venue"):
        out({"verdict": "venue-failed", "run_id": j.run_id, "why": (v or {}).get("why") or "the venue did not stand"}, 2)
    vfacts = {"Contract items": contract["items"], "The venue": standin.venue_facts(v), "Goal": ask}
    try:
        res = drive.run(j, vfacts, v, wt, base_sha, slug, _seat_kw(a, evidence), cfg(), ["ASK"])
    finally:
        if not a.keep_venue:
            standin.venue_down(j, v["venue"], wt, key=f"venue-down-audit-{gitops.head_of(wt)[:9]}")
    rows = ((res or {}).get("report") or {}).get("items") or []
    receipt = {"verdict": (res or {}).get("result"), "run_id": j.run_id, "ask": ask, "tip": gitops.head_of(wt),
               "venue": standin.venue_facts(v).get("origin"), "rows": rows, "contract": NO_CONTRACT}
    (evidence / "audit-here.json").write_text(json.dumps(receipt, indent=1, ensure_ascii=False), encoding="utf-8")
    out(receipt, 0 if receipt["verdict"] == "ok" else 2)


def cmd_land(a):
    """The record and the PR from the receipts the verbs collected, through the one lander the beats use."""
    from hb.beats import landing
    wt, slug, j = _here()
    base_sha = j.fact("base_sha") or ""
    branch = BRANCH_PREFIX + slug
    evidence = _evidence(j)
    ask = a.ask or j.fact("ask") or "the change in this worktree"
    contract = _ask_contract(slug, ask)
    _open, review_report = loop.open_findings(j)
    audit = {}
    if (evidence / "audit-here.json").exists():
        audit = json.loads((evidence / "audit-here.json").read_text(encoding="utf-8"))
    receipts = audit.get("rows") or [{"item": "ASK", "state": "UNMEASURED", "substate": "not-driven",
                                      "why": "no audit ran; `hb audit --here` drives the change where the venue offers ui"}]
    st = {"gates": [], "preexisting": [], "review_report": review_report, "receipts": receipts, "venue": None, "codex_in": {}}
    run_args = SimpleNamespace(shadow=a.shadow, keep_venue=True, wave=None)
    stops = []
    verdict = landing.land(j, run_args, cfg(), contract, wt, branch, base_sha, slug, evidence, st, "here",
                           {"contract": NO_CONTRACT}, None, stops)
    verdict["contract"] = NO_CONTRACT
    out(verdict, 0)


def register(sub):
    s = sub.add_parser("worktree", help="a worktree a session works in, off the landing branch: hb worktree open <slug>")
    s.add_argument("op", choices=["open"])
    s.add_argument("slug", help="a short name; the branch is here/<slug>")
    s.add_argument("--base", help="the ref to branch from (default origin/<branches.landing>)")
    for name, help_ in (("review", "one reviewer seat on this worktree's diff against your ask: hb review --here \"<ask>\""),
                        ("audit", "the venue up and one audit seat driving this worktree's change: hb audit --here")):
        r = sub.add_parser(name, help=help_)
        r.add_argument("ask", nargs="?" if name == "audit" else None, help="what the change is meant to do, in a sentence")
        r.add_argument("--here", action="store_true", required=True, help="on the worktree you stand in")
        r.add_argument("--runtime", help="claude|codex|fake (default: config seats.<role>)")
        r.add_argument("--seat-timeout-min", dest="seat_timeout_min", type=int, default=DEFAULT_SEAT_TIMEOUT_MIN)
        r.add_argument("--rate-limit-wait-min", dest="rate_limit_wait_min", type=int, default=15)
        r.add_argument("--keep-venue", dest="keep_venue", action="store_true", help="audit: leave the venue standing")
    l = sub.add_parser("land", help="the record and the PR from this worktree's receipts, against the landing branch: hb land")
    l.add_argument("ask", nargs="?", help="what the change is meant to do, in a sentence (default: the last review's)")
    l.add_argument("--shadow", action="store_true", help="the record and the PR body, no push and no PR")


def cmd_worktree(a):
    {"open": cmd_worktree_open}[a.op](a)


def cmd_review(a):
    cmd_review_here(a)


def cmd_audit(a):
    cmd_audit_here(a)
