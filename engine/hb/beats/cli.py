"""hb.cli - the verb table: argument parsing, and the dispatch to each seam."""

import argparse

from hb.rig.gh import cmd_gh
from hb.record.docket import _CAUSE_CLASSES, _DOCKET_LANES, _DOCKET_TYPES, _DOCKET_VERDICTS, cmd_docket
from hb.record.ledger import _LEDGER_STATES, cmd_ledger
from hb.gates.gates import cmd_run_gate
from hb.gates.ci import cmd_ci_watch, cmd_deploy_watch
from hb.beats.worktrees import cmd_branch_gc, cmd_tidy, cmd_worktree_gc
from hb.record.page import cmd_page, register as _page_register
from hb.beats.closeout import cmd_closeout, register as _closeout_register
from hb.beats.demo import cmd_demo_launch, register as _demo_launch_register
from hb.contract.rules import cmd_contract, register as _contract_register
from hb.record.sittings import cmd_sittings, register as _sittings_register
from hb.venue.venue import cmd_venue, register as _venue_register
from hb.rig.rig import cmd_rig, register as _rig_register
from hb.venue.devshops import cmd_shops as cmd_dev_venues, register_verb as _dev_venues_register
from hb.beats.run import cmd_run, register_run
from hb.beats import here as _here
from hb.beats import verbs


                                     # design and review stay legal for older records


# ── main ──────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(prog="hb")
    sub = ap.add_subparsers(dest="cmd", required=True)


    s = sub.add_parser("branch-gc", help="branch hygiene: PR-state-based (squash-blind-"
                                         "proof) sweep of remote+local branches; dry-run "
                                         "by default, --apply to delete")
    s.add_argument("--apply", action="store_true")


    s = sub.add_parser("run-gate", help="sanctioned gate runner: no shell, no "
                                        "pipeline; captures output to evidence/gates/, "
                                        "prints TRUE_EXIT, exits with the command's code")
    s.add_argument("--issue"); s.add_argument("--timeout-min", dest="timeout_min", type=int)
    s.add_argument("--tz", help="the TZ the gate's child runs under (default UTC, which "
                                "is CI's clock). `--tz machine` keeps the host clock. The "
                                "artifact records TZ= either way. Windows note: Python "
                                "honours TZ=UTC but not IANA names, and Node reads TZ only "
                                "from inside the process")
    s.add_argument("gateargs", nargs=argparse.REMAINDER,
                   help="the gate command after -- (run directly, never via a shell)")


    s = sub.add_parser("ci-watch"); s.add_argument("--pr", required=True)
    s.add_argument("--timeout-min", type=int, default=45)

    s = sub.add_parser("deploy-watch"); s.add_argument("--timeout-min", type=int, default=20)
    s.add_argument("--sha", default="", help="merge SHA, pins the run (required under concurrent waves)")
    s.add_argument("--branch", default="",
                   help="the ref the Deploy run was dispatched on (a wave branch for the "
                        "pre-merge composed drive); default: the default branch")


    s = sub.add_parser("worktree-gc", help="sweep merged-branch worktrees (dry-run unless --apply)")
    s.add_argument("--apply", action="store_true", help="actually remove; default is dry-run")
    s.add_argument("--include-untracked", action="store_true",
                   help="also sweep merged worktrees whose only dirt is untracked files (agent scratch)")

    s = sub.add_parser("tidy", help="sweep what finished runs left: stray stand-ins, landed worktrees and "
                                    "landed branches (dry-run unless --apply)")
    s.add_argument("--apply", action="store_true", help="actually remove; default is dry-run")
    s.add_argument("--older-than-hours", dest="older_than_hours", type=float, default=12.0,
                   help="age at which a stand-in with no closed owner is an orphan")

    s = sub.add_parser("gh", help="mutex-guarded gh passthrough: hb gh -- <gh args>")
    s.add_argument("ghargs", nargs=argparse.REMAINDER,
                   help="args passed verbatim to gh (prefix with -- to stop option parsing)")

    s = sub.add_parser("docket", help="the owner's queue: single home, verdict-only exit")
    s.add_argument("op", choices=["add", "list", "verdict", "retype"])
    s.add_argument("--needs-ruling", dest="needs_ruling", action="store_true",
                   help="add: this WORK item also needs the owner's word before the "
                        "work; keeps its lane, the walk shows the flag")
    s.add_argument("--link", help="add: an open item at the same file:line that this one "
                                  "is a DIFFERENT cause from (lifts the duplicate refusal)")
    s.add_argument("--type", help=f"add/retype: one of {_DOCKET_TYPES}")
    s.add_argument("--lane", help=f"add (REQUIRED, docket/v3) / retype / list: one of {_DOCKET_LANES}. "
                                  "hotfix = one territory, no migration/dep/request-field/auth "
                                  "change, no mock; slice = known shape, fails a hotfix gate; "
                                  "sitting = taste or a new surface; ruling = RULE/CALL only "
                                  "(set automatically for those types)")
    s.add_argument("--cause", help="add (REQUIRED, docket/v3) / retype: the MEASURED cause, a "
                                   "file:line, a probe result, a driven behavior. WHY it happens, "
                                   "never what the owner sees (that is --plain)")
    s.add_argument("--desired", help="add (REQUIRED, docket/v3) / retype: the state that SHOULD "
                                     "exist, citing the ruling / register line / manifest item / "
                                     "design record it must satisfy")
    s.add_argument("--cause-class", dest="cause_class",
                   help=f"add (REQUIRED, docket/v3) / retype: one of {_CAUSE_CLASSES}")
    s.add_argument("--recurrence", help="add / retype: sibling hits of the same cause "
                                        "(the grep and its hits), or 'none: <what was swept>'")
    s.add_argument("--title"); s.add_argument("--body"); s.add_argument("--rec")
    s.add_argument("--plain", help="add: REQUIRED, what the owner would notice, the cost of doing nothing, "
                                   "and the recommendation, in plain words")
    s.add_argument("--wave"); s.add_argument("--discovered-by", dest="discovered_by")
    s.add_argument("--id"); s.add_argument("--verdict", help=f"one of {_DOCKET_VERDICTS}")
    s.add_argument("--note"); s.add_argument("--revisit", help="REQUIRED with --verdict parked")
    s.add_argument("--legacy-id", dest="legacy_id")
    s.add_argument("--action-file", dest="action_file",
                   help="path to a prepared action (draft issue body / ADR text / slice spec)")
    s.add_argument("--status", help="list: filter by status")


    s = sub.add_parser("ledger", help="the delivery ledger, receipt (append one "
                                      "row to the slice's receipts.jsonl) | append "
                                      "(SINGLE WRITER: a wave's receipts -> the "
                                      "tracked JSONL) | query | validate")
    s.add_argument("op", choices=["receipt", "append"])
    s.add_argument("--issue"); s.add_argument("--wave"); s.add_argument("--slug")
    s.add_argument("--item"); s.add_argument("--rev", type=int)
    s.add_argument("--state", help=f"one of {_LEDGER_STATES}")
    s.add_argument("--substate", help="BROKEN: failed|mismatch|not-built|infeasible "
                                      "· UNMEASURED: not-driven|verifier-failed|"
                                      "stack-failed")
    s.add_argument("--ruling", help="DIVERGED: the docket id asking the owner "
                                    "to rule between the manifest and what was built")
    s.add_argument("--anchor", help="the wave's anchor issue. append: its receipts file "
                                    "(a composed drive filing on the anchor) is read too. "
                                    "receipt: REQUIRED for a G<n> item: the id is checked "
                                    "against the anchor's `## GOAL` block, not the manifest")
    s.add_argument("--sha", help="the commit SHA the row was MEASURED at")
    s.add_argument("--venue", help="receipt: the venue id the item was driven on (hb venue up)")
    s.add_argument("--evidence", action="append",
                   help="evidence path (repeatable); must EXIST on disk")
    s.add_argument("--observed", help="what was observed (required in spirit for "
                                      "every red/grey row)")
    s.add_argument("--situation",
                   help="receipt: what the verifier BUILT before driving this item (the "
                        "fixture recipe made real). Required for WORKS and BROKEN; a row "
                        "without it is untested by rule")
    s.add_argument("--plan", help="receipt: path of the verifier's test plan file "
                                  "(items/<id>/plan.md) this row was driven against")
    s.add_argument("--tenant-id", dest="tenant_id",
                   help="receipt: the tenant the substance read was taken against. "
                        "Refused rather than defaulted.")
    s.add_argument("--no-venue", dest="no_venue",
                   help="receipt: why no venue was read (a tree scan or a unit pin), in "
                        "place of --tenant-id; refused on a venue-driven kind "
                        "(screen, form, list, chat-verb, money, api)")
    s.add_argument("--served-sha", dest="served_sha",
                   help="receipt: the commit the venue served, from the venue record's "
                        "receipt; never probed. Absent, the row names no served commit.")
    s.add_argument("--widths", help="receipt: the viewport widths driven "
                                    "(default 375,1280)")
    s.add_argument("--timeout", type=int, help="receipt: /version probe timeout s")
    s.add_argument("--venue-url", dest="venue_url",
                   help="receipt: the URL that was ACTUALLY DRIVEN (the "
                        "slice's own venue). env_identity is probed from THIS "
                        "url's /version, never from the deployed tier's address. "
                        "Absent, env identity is UNMEASURED and the receipt says so.")
    s.add_argument("--source", choices=["slice", "composed"],
                   help="receipt: which drive produced this row (default slice). "
                        "`append` resolves a composed row over a slice row for the "
                        "same item id.")
    s.add_argument("--declared-out", dest="declared_out",
                   help="receipt: file UNMEASURED/not-driven for an item declared "
                        "out of the wave, with this REASON, without tenant or evidence "
                        "(demanding attribution for an absence forces fabrication)")
    s.add_argument("--issues",
                   help="append: comma-separated issue numbers whose receipts files to fold")


    # seams that register their own verbs
    _venue_register(sub)
    _rig_register(sub)
    _dev_venues_register(sub)
    register_run(sub)
    _here.register(sub)
    _page_register(sub)
    _closeout_register(sub)
    _demo_launch_register(sub)
    _contract_register(sub)
    _sittings_register(sub)

    verbs.check_registered(sub)
    a = ap.parse_args()
    {
     "venue": cmd_venue, "rig": cmd_rig, "venues": cmd_dev_venues,
     "ci-watch": cmd_ci_watch, "deploy-watch": cmd_deploy_watch, "gh": cmd_gh,
     "worktree-gc": cmd_worktree_gc, "tidy": cmd_tidy, "page": cmd_page, "closeout": cmd_closeout, "demo-launch": cmd_demo_launch, "contract": cmd_contract, "sittings": cmd_sittings, "docket": cmd_docket,
     "run-gate": cmd_run_gate, "branch-gc": cmd_branch_gc,
     "ledger": cmd_ledger,
     "run": cmd_run, "worktree": _here.cmd_worktree, "review": _here.cmd_review,
     "audit": _here.cmd_audit, "land": _here.cmd_land}[a.cmd](a)
