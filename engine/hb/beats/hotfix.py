"""hb.run.hotfix - the hotfix door, and the `hb run` verb that opens every door.

    hb run hotfix "<one sentence>" | #<issue>  [--runtime claude|codex|fake]
                                                [--shadow] [--resume <run_id>]

Stages, in a fixed order the runner walks and records in the run journal:

  contract   the one-item contract from the sentence or the issue body
  worktree   a fresh tree on hotfix/<slug> from origin/<default>
  before     the screens' look at the base commit (loop.capture_before), unless --no-venue
  build      the builder on its own venue, relaunched on its progress file (slice.build)
  authority  stop on the note's needs_authority rows
  loop       the gates, the review, the checks and the builder relaunched on what they found,
             then the blind audit (loop.py), the same loop the slice door runs
  land       the record, the PR body and the PR (landing.land)
  standing   the run stays up for the chat's next step or the merge (standing.py)

`--shadow` runs every stage but pushes nothing and opens no PR, so the runner can
be measured beside the current command on the same change. `--resume` re-enters at
the recorded phase; completed operations are not repeated.
"""

import sys
import re
from pathlib import Path

from hb.rig import versions
from hb.rig.common import canon, cfg, out, state_dir
from hb.record import control
from hb.contract import policy
from hb.venue import adapter
from hb.runner import stops as stop_ids
from hb.rig import gitops
from hb.gates import gating
from hb.beats import landing
from hb.record import record
from hb.contract import contract as resolver
from hb.beats import slice as slice_door
from hb.beats import loop
from hb.beats import launch
from hb.beats import standing
from hb.record import journal
from hb.record.journal import Journal, new_run_id, slugify


DEFAULT_SEAT_TIMEOUT_MIN = policy.SEAT_FUSE_MIN  # the fuse, never the cut


def run_hotfix(a):
    c = cfg()
    root = canon()
    j = Journal(a.resume) if a.resume else Journal(new_run_id("hotfix", a.ref))
    j.open_run("hotfix", migrate=a.migrate_harness, ref=a.ref, shadow=bool(a.shadow), runtime=a.runtime)
    slug = j.fact("slug") or slugify(a.ref if not re.match(r"^#?\d+$", a.ref.strip()) else f"issue-{a.ref.strip('#')}")
    evidence = state_dir() / "runs" / j.run_id / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    stops = []
    seat_kw = dict(runtime=a.runtime, timeout_min=a.seat_timeout_min, shadow=a.shadow, evidence=evidence,
                   rate_limit_wait_min=a.rate_limit_wait_min)

    # contract
    contract = resolver.for_run(j, a.ref, evidence)
    if contract["source"] == "issue" and not contract["items"] and contract["problems"]:
        j.phase("stopped", why=contract["problems"][0])
        return out({"verdict": "stopped", "why": contract["problems"][0], "run_id": j.run_id}, 2)
    j.phase("contract", slug=slug, issue=contract["issue"], goal=contract["goal"])
    launch.announce(j, a, c, contract)
    if a.resume and standing.saved(j):
        return standing.resume(j, a, c, contract, _lander(j, a, c, contract, standing.saved(j), evidence), seat_kw)

    # worktree
    tree = gitops.worktree(j, slug, root, c, a.shadow, prepare=adapter.prepare_worktree)
    if tree.get("error"):
        j.phase("stopped", why=tree["error"])
        return out({"verdict": "stopped", "why": tree["error"], "run_id": j.run_id}, 2)
    wt, branch, base_sha = Path(tree["worktree"]), tree["branch"], tree["base_sha"]
    j.phase("worktree", worktree=str(wt), branch=branch, base_sha=base_sha)

    # the screens' look at the base commit, then the builder on its own venue
    before = loop.capture_before(j, a, contract["items"], wt, slug, base_sha, evidence, seat_kw, contract["issue"])
    note = slice_door.resumed(j)["note"]
    if note is None:
        own = slice_door.venue_for_build(j, a, contract, slug, wt, {}, "1", gitops.changed_paths(wt, base_sha))
        built, note = slice_door.build(j, contract, wt, base_sha, seat_kw, own=own)
        slice_door.builder_venue_down(j, wt, own)
    if note is None:
        j.phase("stopped", why=f"build seat: {built['result']}", problems=built.get("problems"))
        return out({"verdict": "stopped", "stage": "build", "why": built["result"],
                    "problems": built.get("problems"), "run_id": j.run_id}, 2)
    j.phase("build", tip=gitops.head_of(wt))
    # an authority stop answered by its id reached this build seat in its brief; it goes on
    stop_ids.authority(j, stops, note)
    if stops:
        j.phase("stopped", why="needs authority", stops=stops)
        path = record.write_local(j, wt, contract, evidence, base_sha, note, stops)
        return out({"verdict": "stopped", "stage": "authority", "stops": stops, "run_id": j.run_id, "record": path}, 2)

    st = loop.run_loop(j, a, c, contract, wt, branch, base_sha, slug, note, evidence, stops, seat_kw, before=before)
    res = _land(j, a, c, contract, wt, branch, base_sha, slug, evidence, note, before, stops, st)
    ctx = standing.context("hotfix", a.ref, wt, branch, base_sha, slug, note, before, st, res)
    return standing.after_ship(j, a, c, contract, ctx, _lander(j, a, c, contract, ctx, evidence), res,
                               0 if not stops else 2, seat_kw)


def _lander(j, a, c, contract, ctx, evidence):
    """The landing a standing pass calls: the same report, PR refresh and Walk."""
    return lambda st, note, stops: _land(j, a, c, contract, Path(ctx["worktree"]), ctx["branch"], ctx["base_sha"],
                                         ctx["slug"], evidence, note, ctx["before"], stops, st)


def _land(j, a, c, contract, wt, branch, base_sha, slug, evidence, note, before, stops, st):
    """A hotfix books nothing of its own; the one lander (landing.land) does the rest."""
    return landing.land(j, a, c, contract, wt, branch, base_sha, slug, evidence, st, "hotfix", note, before, stops)


def _opened_on(a):
    """The harness version a run being resumed opened on, read from its journal, or None."""
    if not getattr(a, "resume", None):
        return None
    # --resume also names the run id of a first run (the drills do); no journal yet means the current
    # pin, and a journal that cannot be read raises rather than passing as new
    return Journal(a.resume).fact("harness")


def _resume_cmd(a, j):
    """The line that re-enters this run where it stopped: a slice repair keeps its --repair."""
    repair = f" --repair {a.repair}" if getattr(a, "repair", None) and a.kind == "slice" else ""
    return f"hb run {a.kind} {a.ref!r} --resume {j.run_id}{repair} --detach"


def cmd_run(a):
    if a.kind == "watch":
        from hb.record.watch import cmd_watch
        return cmd_watch(a)
    if a.kind == "next":
        from hb.beats.standing import cmd_next
        return cmd_next(a)
    if a.kind == "grade":
        from hb.record.grade import cmd_run_grade
        return cmd_run_grade(a)
    if a.kind in ("hotfix", "slice", "wave"):
        entry = versions.pin_self(_opened_on(a))
        if entry:
            raise SystemExit(versions.reenter(entry, sys.argv[1:]))
        problems = adapter.check_config()
        if problems:
            out({"verdict": "refused-venue-config", "why": "the venue block cannot be run; no seat starts", "problems": problems}, 2)
    doors = {"hotfix": run_hotfix}
    if a.kind == "slice":
        from hb.beats.slice import run_slice
        doors["slice"] = run_slice
    if a.kind == "wave":
        try:
            from hb.beats.wave import run_wave
        except ImportError:
            # a cut of the engine that ships one beat has no wave door; say so, never guess
            out({"verdict": "not-in-this-cut", "why": "this engine ships no wave door"}, 2)
        doors["wave"] = run_wave
    if a.kind in doors and getattr(a, "detach", False):
        from hb.runner.detach import launch_detached
        return launch_detached(a)
    if a.kind in doors:
        try:
            return doors[a.kind](a)
        except journal.HarnessMoved as m:
            # a resume on another pin runs new code over old events; it refuses by name
            out({"verdict": "harness-moved", "run_id": m.run_id, "opened_on": m.opened_on, "now_on": m.now_on,
                 "why": str(m)}, 2)
        except control.UsageLimited as lim:
            j, stops = lim.journal, []
            resume = _resume_cmd(a, j)
            stop_ids.raise_stop(j, stops, "limit", stop_ids.limit_id(lim.text), f"usage limit: {lim.text}")
            j.phase(journal.PAUSED_LIMIT, why=f"the {lim.seat} seat met the plan's usage limit; resets {lim.resets}",
                    stops=stops, seat=lim.seat, resets=lim.resets, resets_at=lim.resets_at, resume=resume)
            out({"verdict": journal.PAUSED_LIMIT, "run_id": j.run_id, "seat": lim.seat, "resets": lim.resets,
                 "resets_at": lim.resets_at, "stops": stops, "journal": str(j.path), "resume": resume}, 3)
        except control.Paused as p:
            j = p.journal
            j.phase("paused", why="the conductor asked; --resume re-enters at this boundary")
            out({"verdict": "paused", "run_id": j.run_id, "journal": str(j.path),
                 "resume": f"hb run {a.kind} {a.ref!r} --resume {j.run_id}"}, 3)
        except gating.RigFault as f:
            j, stops = f.journal, []
            resume = f"hb run {a.kind} {a.ref!r} --resume {j.run_id} --detach"
            stop_ids.raise_stop(j, stops, "rig", stop_ids.other_id(f.why),
                                f"the runner lost its tools ({f.why}); no fixer was started. "
                                f"Resume with `{resume}`")
            j.phase("stopped", why="rig fault", stops=stops)
            out({"verdict": "stopped", "stage": "rig", "stops": stops, "run_id": j.run_id,
                 "journal": str(j.path), "resume": resume}, 2)
    out({"verdict": "not-built-yet", "kind": a.kind,
         "use": "sweep is the one kind the runner does not run yet; /sweep launches one "
                "hb run hotfix per issue"}, 3)


def register_run(sub):
    s = sub.add_parser("run", help="the runner: hb run hotfix <sentence|#issue> [--shadow] [--resume ID]; hb run grade <run id>")
    s.add_argument("kind", choices=("hotfix", "slice", "wave", "sweep", "rig", "watch", "next", "grade"))
    s.add_argument("ref", help="a sentence in quotes, or #<issue>; for watch and next, the run id")
    s.add_argument("words", nargs="*", help="next: the verb (build, fix, door, drive, sync) and its text")
    s.add_argument("--after", type=int, default=0, help="watch: the journal cursor the last watch printed")
    s.add_argument("--max-min", dest="max_min", type=float, default=50,
                   help="watch: return with nothing new after this long, so a watcher never hangs")
    s.add_argument("--runtime", help="claude|codex|fake for every seat (default: config seats.<role>)")
    s.add_argument("--shadow", action="store_true", help="every stage, but no push and no PR")
    s.add_argument("--resume", help="re-enter a run at its recorded phase")
    s.add_argument("--migrate-harness", dest="migrate_harness", action="store_true",
                   help="resume a run opened on another harness pin: the move is journaled and every receipt is owed")
    s.add_argument("--detach", action="store_true",
                   help="start the run as its own process apart from this shell; print its run id and return")
    s.add_argument("--repair", metavar="RUN_ID",
                   help="slice or wave: re-enter a finished run's branch at its red rows and its last "
                        "review's open findings; the repair loop runs, no build and no whole-branch review, "
                        "and the existing PR is refreshed")
    s.add_argument("--fresh", action="store_true",
                   help="with --repair on a wave: carry no row and no finding; the composed loop runs on the "
                        "branch's tip as a first pass would, the whole-diff review first")
    s.add_argument("--brief", help="a line for every seat, recorded as the first control answer at launch")
    s.add_argument("--no-venue", dest="no_venue", action="store_true", help="skip the stand-in and the verifier")
    s.add_argument("--no-before", dest="no_before", action="store_true", help="skip the before captures at the base commit")
    s.add_argument("--wave", help="slice: spawned by this wave; lands on the wave branch, no PR of its own")
    s.add_argument("--real-llm", dest="real_llm", action="store_true", help="the stand-in runs the real model")
    s.add_argument("--keep-venue", dest="keep_venue", action="store_true")
    s.add_argument("--seat-timeout-min", dest="seat_timeout_min", type=int, default=DEFAULT_SEAT_TIMEOUT_MIN,
                   help="the fuse on one seat; the cut is the stall window in adapters.STALL_MIN")
    s.add_argument("--rate-limit-wait-min", dest="rate_limit_wait_min", type=int, default=15)
