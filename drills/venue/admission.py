#!/usr/bin/env python
"""Drill for the host-wide admission door (hb.admission, resource ruling 13): a start is
refused under the line and enters when the room returns; a running grant under the line
blocks new starts and nothing waits forever; an empty host forces a start on a fresh reading;
starts settle one at a time and concurrent claimants serialize; growth inside the settle
window holds the next start; a reading the host refuses counts as under the line; a fan-out
is sized to the room above the line; a killed holder is reclaimed; a child's bytes are
measured; a child under a grant takes no second grant; a held grant ends on any thread. Run:

    python engine/drill_admission.py

In-process on a throwaway repo; the host's readings are fixed through HB_MEMORY_FREE_GB and
HB_MEMORY_TOTAL_GB per fault, and every rule that turns on time is asked on a fake clock
(`admission._decide`, `admission._settle_done` take `now`), so the checks read the engine's
rule and never this host's load or seconds.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ALONE = True  # times the host's own waits; runs after the parallel set
HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
FIXTURE = ROOT / "drills" / "fixture"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))
from fixture.common import PASS, FAIL, check, make_repo  # noqa: E402,F401
from hb.venue import admission  # noqa: E402
from hb.venue import machine  # noqa: E402

# a 32 GB host: the line is 3.2 GB
C = {"state_dir": ".context/harness", "machine": {"poll_s": 0.05, "stale_after_s": 60,
                                                   "settle_s": 0.3, "settle_max_s": 2, "force_after_s": 0.5}}
GB = 1 << 30
os.environ["HB_MEMORY_TOTAL_GB"] = "32"


def fixed(gb):
    os.environ["HB_MEMORY_FREE_GB"] = str(gb)


def notes_of():
    notes = []
    return notes, (lambda event, **f: notes.append(dict(f, event=event, at=time.time())))


def events(notes, state, holder=None):
    return [n for n in notes if n["event"] == "admission" and n["state"] == state and (holder is None or n["holder"] == holder)]


LINE = int(admission.LINE * 32 * GB)
FORCE = 90.0  # force_after_s on the fake clock


def _doc(grants=(), waiting=("me",), since=0.0, empty_since=None, starting=None):
    """An admission record on the fake clock: `grants` and `waiting` name holders."""
    doc = {"grants": {f"g-{h}": {"kind": "gate:suite", "holder": h} for h in grants},
           "waiting": {f"w-{h}": {"kind": "seat:verify", "holder": h, "since": since} for h in waiting},
           "starting": starting, "empty_since": empty_since}
    return doc


def drill_under_the_line_waits_and_enters_when_room_returns(repo):
    """Free memory under the line with a grant standing: the claimant waits with the reason,
    whatever the clock reads, and enters once a reading rises above the line, not forced."""
    doc = _doc(grants=["run-a suite"])
    ok, forced, why = admission._decide(doc, "w-me", 1 * GB, LINE, now=FORCE * 10, force_after_s=FORCE)
    check("under the line: waits while a grant stands, long past the forced wait",
          not ok and "under the line" in why, why)
    ok, forced, why = admission._decide(doc, "w-me", 64 * GB, LINE, now=FORCE * 10, force_after_s=FORCE)
    check("under the line: enters once the room returns, not forced", ok and forced is False and "above the line" in why, why)


def drill_a_running_grant_under_the_line_blocks_starts_and_nothing_waits_forever(repo):
    """A running grant under the line blocks every new start; once it ends and the host is still
    under the line, the claimant is the empty host's and is forced after its wait, counted from
    the later of its arrival and the host emptying."""
    ok, _f, _w = admission._decide(_doc(grants=["run-b suite"]), "w-me", 1 * GB, LINE, now=1e6, force_after_s=FORCE)
    check("running under the line: a new start waits while a grant runs, however long", not ok)
    empty = _doc(since=0.0, empty_since=500.0)
    ok, forced, _w = admission._decide(empty, "w-me", 1 * GB, LINE, now=500.0 + FORCE - 1, force_after_s=FORCE)
    check("running under the line: just after the grant ends, the host is given its wait", not ok)
    ok, forced, why = admission._decide(empty, "w-me", 1 * GB, LINE, now=500.0 + FORCE, force_after_s=FORCE)
    check("running under the line: then the empty host forces the start", ok and forced is True and "empty host" in why, why)


def drill_a_light_workload_never_waits(repo):
    """166 checks once paid 90 s each on a host whose line is never met: a light kind is admitted at once."""
    fixed(1)
    notes = []
    t0 = time.time()
    with admission.take("checks", "drill checks", note=lambda ev, **f: notes.append(dict(f, ev=ev))) as grant:
        waited = time.time() - t0
        check("light: a checks grant under the line is admitted in under a second", grant is not None and waited < 1.0, f"{waited:.2f}s")
    granted = [n for n in notes if n.get("state") == "granted"]
    check("light: the grant says why", granted and "light workload" in str(granted[0].get("why")), str(granted[:1])[:200])
    t0 = time.time()
    with admission.take("checks", "drill checks 2", note=lambda ev, **f: None):
        check("light: a second light start is not held behind a settle", time.time() - t0 < 1.0)


def drill_an_empty_host_forces_a_start_on_a_fresh_reading(repo):
    """Nothing of ours runs and the host is under the line: after the forced wait the claimant
    starts anyway, and the grant names a fresh reading."""
    ok, _f, _w = admission._decide(_doc(since=0.0), "w-me", 1 * GB, LINE, now=FORCE - 1, force_after_s=FORCE)
    check("empty host: not before the forced wait", not ok)
    ok, forced, why = admission._decide(_doc(since=0.0), "w-me", 1 * GB, LINE, now=FORCE, force_after_s=FORCE)
    check("empty host: forced at the wait", ok and forced is True, why)
    notes, note = notes_of()
    fixed(1)
    now = dict(C, machine=dict(C["machine"], force_after_s=0))
    with admission.take("gate:lint", "run-c lint", now, note):
        pass
    granted = events(notes, "granted", "run-c lint")
    check("empty host: the grant is forced and names the fresh reading",
          granted and granted[0]["forced"] is True and "fresh reading of" in granted[0]["why"]
          and "empty host" in granted[0]["why"], str(granted))


def drill_concurrent_claimants_serialize_through_the_settle(repo):
    """A claimant above the line waits behind a start that is still settling, by name; one start
    at a time across every run on the host."""
    settling = {"token": "g-run-d build.0", "holder": "run-d build.0"}
    doc = _doc(grants=["run-d build.0"], starting=settling)
    ok, _f, why = admission._decide(doc, "w-me", 64 * GB, LINE, now=1.0, force_after_s=FORCE)
    check("serialize: a claimant waits behind the start that is still settling, named", not ok
          and "behind run-d build.0, which is still settling" in why, why)
    doc["starting"] = None
    ok, _f, _w = admission._decide(doc, "w-me", 64 * GB, LINE, now=1.0, force_after_s=FORCE)
    check("serialize: once it settled the next enters", ok)


def drill_growth_inside_the_window_holds_the_next_start(repo):
    """A start that keeps growing, with pauses shorter than the settle window, holds the next
    claimant until it stops; a start that never stops holds it only until settle_max_s."""
    settle, most = 30.0, 180.0
    done = lambda now, last: admission._settle_done(now, born=0.0, last_growth=last, settle_s=settle, settle_max_s=most)
    check("growth: a start that grew inside the window is still settling", not done(100.0, 100.0 - settle + 1))
    check("growth: quiet for the window, it has settled", done(100.0, 100.0 - settle))
    check("growth without end: settled at settle_max_s, however recent the growth", done(most, most) and not done(most - 1, most - 1))


def drill_an_unreadable_host_counts_as_under_the_line(repo):
    """A reading the host refuses is under the line: a claimant waits while a grant stands,
    and says the host did not answer; on an empty host the forced wait still lets it in."""
    ok, _f, why = admission._decide(_doc(grants=["run-g suite"]), "w-me", None, LINE, now=1.0, force_after_s=FORCE)
    check("unreadable: waits while a grant stands, and says the host did not answer",
          not ok and "did not answer" in why, why)
    ok, forced, why = admission._decide(_doc(since=0.0), "w-me", None, LINE, now=FORCE, force_after_s=FORCE)
    check("unreadable: the empty host forces it after the wait", ok and forced is True, why)


def drill_a_fan_out_is_sized_to_the_room_above_the_line(repo):
    """A suite that fans out gets one worker per gigabyte above the line, never under its
    least nor over its most; a forced start gets its least."""
    notes, note = notes_of()
    fixed(10)  # 6.8 GB above a 3.2 GB line
    with admission.take("gate:suite", "run-h suite", C, note, units=(2, 16)) as g1:
        pass
    time.sleep(0.4)
    fixed(64)
    with admission.take("gate:suite", "run-h suite2", C, note, units=(2, 16)) as g2:
        pass
    time.sleep(0.4)
    fixed(1)
    with admission.take("gate:suite", "run-h suite3", C, note, units=(2, 16)) as g3:
        pass
    check("sized: 10 GB free gives 6 workers", g1["units"] == 6, str(g1))
    check("sized: never over the most", g2["units"] == 16, str(g2))
    check("sized: a forced start gets the least", g3["units"] == 2, str(g3))


def drill_killed_holder_is_reclaimed(repo):
    """A holder that dies with its grant is swept by the next claimant, journaled, and the room returns."""
    notes, note = notes_of()
    fixed(64)
    child = subprocess.Popen([sys.executable, "-c", (
        f"import sys, time; sys.path.insert(0, {str(ENGINE)!r}); import os; os.environ['HB_MEMORY_FREE_GB'] = '64'\n"
        f"from hb.venue import admission\n"
        f"with admission.take('venue', 'doomed', {C!r}):\n"
        f"    time.sleep(60)")], cwd=str(repo))
    path = admission._paths(C)[1]
    for _ in range(200):
        if any(g["holder"] == "doomed" for g in admission._load(path)["grants"].values()):
            break
        time.sleep(0.05)
    held = [g["holder"] for g in admission._load(path)["grants"].values()]
    child.kill()
    child.wait()
    with admission.take("venue", "next", C, note):
        pass
    reclaimed = events(notes, "reclaimed")
    check("killed holder: the child held its grant", held == ["doomed"], str(held))
    check("killed holder: the next claimant reclaims it and is journaled",
          reclaimed and reclaimed[0]["holder"] == "doomed" and "not running" in reclaimed[0]["why"], str(reclaimed))
    check("killed holder: the next claimant enters without a wait journaled", not events(notes, "waiting"), str(notes)[-300:])


def drill_child_bytes_are_recorded(repo):
    """A child the body spawns is measured through its process tree and its peak journaled
    at release, within ten percent of what it allocated."""
    notes, note = notes_of()
    fixed(64)
    n = 400 << 20
    with admission.take("alloc", "measurer", C, note):
        p = subprocess.Popen([sys.executable, "-c", f"b = b'x' * {n}; import time; time.sleep(0.6)"])
        admission.watch(p.pid)
        p.wait()
    rel = events(notes, "released")
    peak = rel[0]["peak"] if rel else None
    check("child bytes: the peak is journaled within ten percent of the allocation",
          peak is not None and n <= peak <= n * 1.1, f"peak {peak} for {n}")


def drill_a_child_under_a_grant_takes_no_second_grant(repo):
    """A process the holder spawned under its grant (HB_ADMITTED) takes nothing: it enters at
    once though the host is under the line, journaled under-parent."""
    notes, note = notes_of()
    fixed(64)
    with admission.take("seat:build", "parent", C, note) as grant:
        pass
    check("under a grant: the grant hands its mark to its children", grant["env"] == {admission.HELD_ENV: "parent"}, str(grant))
    with admission.take("seat:build", "parent", C, note) as grant:
        os.environ[admission.HELD_ENV] = grant["env"][admission.HELD_ENV]
        try:
            with admission.take("gate:suite", "nested", C, note) as inner:
                pass
        finally:
            os.environ.pop(admission.HELD_ENV, None)
    under = [n for n in notes if n["event"] == "admission" and n["state"] == "under-parent"]
    check("under a grant: the nested take is journaled under-parent and names the parent",
          under and under[0]["holder"] == "nested" and under[0]["parent"] == "parent" and inner.get("under") == "parent", str(under))


def drill_a_held_grant_ends_on_another_thread(repo):
    """The rehearsal's venue was held on the main thread and taken down in the drive lane's
    thread, where release raised and left the grant (and the venue) standing. A held grant
    ends on any thread, by identity, and a take on the holding thread does not watch it."""
    notes, note = notes_of()
    fixed(64)
    release = admission.hold("venue", "run-x venue-1-1", C, note, sample=lambda: None)
    with admission.take("seat:verify", "run-x verify", C, note, sample=lambda: None) as seat:
        admission.watch(os.getpid())
    raised = []

    def elsewhere():
        try:
            release()
        except Exception as ex:
            raised.append(ex)
    t = threading.Thread(target=elsewhere)
    t.start()
    t.join(timeout=10)
    doc = admission._load(admission._paths(C)[1])
    check("held grant: released on another thread without raising", not raised, str(raised))
    check("held grant: the record holds nothing after it", doc["grants"] == {} and doc["waiting"] == {} and doc["starting"] is None, str(doc))
    check("held grant: watch() named the scoped seat, never the held venue", seat["pid"] == os.getpid(), str(seat))
    check("held grant: no stack is left on the holding thread", getattr(admission._HERE, "stack", []) == [],
          str(getattr(admission._HERE, "stack", None)))


def drill_a_held_venue_does_not_keep_the_host_from_reading_empty(repo):
    """A drive's venue is held for the whole drive; with it standing and the host under the line,
    the testers that need it waited forever, because the empty-host rule counted the venue as
    running. A venue is a stage, not work: the waiting seat is forced after its wait."""
    doc = {"grants": {"g-venue": {"kind": "venue", "holder": "run-s venue-1-1"}},
           "waiting": {"w-me": {"kind": "seat:verify", "holder": "run-s verify", "since": 0.0}},
           "starting": None, "empty_since": 0.0}
    ok, forced, why = admission._decide(doc, "w-me", 1 * GB, LINE, now=FORCE, force_after_s=FORCE)
    check("held venue: the waiting seat is forced on the empty-host rule, the venue standing",
          ok and forced is True and "empty host" in why, why)


DRILLS = [drill_a_held_venue_does_not_keep_the_host_from_reading_empty,
          drill_under_the_line_waits_and_enters_when_room_returns,
          drill_a_running_grant_under_the_line_blocks_starts_and_nothing_waits_forever,
          drill_a_light_workload_never_waits,
          drill_an_empty_host_forces_a_start_on_a_fresh_reading,
          drill_concurrent_claimants_serialize_through_the_settle,
          drill_growth_inside_the_window_holds_the_next_start,
          drill_an_unreadable_host_counts_as_under_the_line,
          drill_a_fan_out_is_sized_to_the_room_above_the_line,
          drill_killed_holder_is_reclaimed, drill_child_bytes_are_recorded,
          drill_a_child_under_a_grant_takes_no_second_grant, drill_a_held_grant_ends_on_another_thread]


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-admission-drill-"))
    repo = make_repo(tmp)
    here = os.getcwd()
    os.chdir(str(repo))
    try:
        for d in DRILLS:
            print(f"drill {d.__name__}")
            try:
                d(repo)
            except Exception as ex:
                FAIL.append(f"{d.__name__}: raised {ex.__class__.__name__}: {ex}")
            for p in machine._dir(C).glob("admission.*"):
                try:
                    p.unlink(missing_ok=True)
                except PermissionError:
                    FAIL.append(f"{d.__name__}: left a thread holding {p.name}")
    finally:
        os.chdir(here)
        shutil.rmtree(tmp, ignore_errors=True)
    for p in PASS:
        print("PASS", p)
    for f in FAIL:
        print("FAIL", f)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    print(f"TRUE_EXIT={0 if not FAIL else 1}")
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()
