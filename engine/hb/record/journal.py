"""hb.run.journal - the run journal: the versioned event log a run is the truth of.

One directory per run under <state>/runs/<run_id>/: `journal.jsonl` (append only, every
event carrying the log version `v`), `phase.json` (a view of the phase events, rewritten
for the page; nothing decides from it), and the seats' briefs and reports beside them.

An operation with an external effect (a push, a PR, a venue, a ledger row, a venue state)
runs through `op()`: it is recorded `started` before the effect and `complete`
after, under an idempotency key. On resume a `started` record with no `complete`
is re-checked through the caller's `check` before the effect is repeated, so a
crash between the effect and its record never does the effect twice. The loop's
own effects (a fix seat, a merge, a receipt) are written as an intent before and a
completion after, and `hb.run.reducer` reconciles an intent that never completed.

The run opens on one harness version (`open_run`); a resume on another pin refuses unless
it is a journaled migration, which owes every receipt taken before it.
"""
import json
import os
import re
import threading
import time

from hb.rig import versions
from hb.rig.common import atomic_write, now_iso, state_dir

from hb.runner import liveness


_APPEND_LOCK = threading.Lock()

# The one list of a run's phases; view, watch, watcher, live and the page read these and keep
# none of their own. FINAL ends a run's line; a STANDING run opened its PR and waits for the
# chat or the merge; DONE is a row the page never reads again.
# the finish words a landed run ends on (firstscreen.finish_state decides which)
MERGEABLE, RUN_COMPLETE = "mergeable", "run-complete"
FINISHED = (MERGEABLE, RUN_COMPLETE)
STOPPED, PAUSED_LIMIT = "stopped", "paused-limit"
# a run that waits on someone: a stop, a hold, the plan's reset, or a process gone
HALTED = (STOPPED, "paused", PAUSED_LIMIT, "ended")
FINAL = FINISHED + ("built", STOPPED, "paused", PAUSED_LIMIT, "merged")
STANDING = "standing"
# a run that landed: finished, standing at its PR, or merged
LANDED = FINISHED + (STANDING, "merged")
DONE = FINAL + ("ended", "closed")
# the page's tone and word for each state a run's row can be in
STATE_LABELS = {"closed": ["t-quiet", "closed"], "running": ["t-run", "running"], "stopped": ["t-warn", "stopped"],
                "paused": ["t-quiet", "held"], "paused-limit": ["t-quiet", "paused: usage limit"],
                "ended": ["t-quiet", "ended"], "standing": ["t-run", "standing"], "merged": ["t-good", "merged"],
                "mergeable": ["t-good", "mergeable"], "run-complete": ["t-warn", "run complete, not mergeable"],
                "built": ["t-quiet", "built"]}
# the activity feed's words for a phase the run moved into
PHASE_WORDS = {"worktree": "Workspace ready", "build": "Build finished", "check": "Closing check started",
               "queue": "Reading the wave's queue", "slices": "Every slice has ended", "compose": "Slices combined on the wave branch",
               "repair": "Repair started", "mergeable": "Finished: mergeable", "run-complete": "Finished: the record is red, not mergeable",
               "built": "Built; the wave measures it", "paused": "Held", "paused-limit": "Paused: usage limit",
               "before": "Before pictures taken", "standing": "Standing: PR open, waiting for the chat or the merge",
               "merged": "The PR merged; the run ended"}
# the log version: a reducer folds only events it knows the shape of
VERSION = 2
# the fault hook: HB_DIE_AT=<kind>[:<fact>=<value>...] kills the process at the boundary of the
# first event it names, before it is written, so the effect landed and its record did not
ENV_DIE_AT = "HB_DIE_AT"


def die(kind):
    """Kill the process at a boundary; the drills swap this to raise in-process."""
    os._exit(137)


def dies_at(event_kind, facts):
    want = os.environ.get(ENV_DIE_AT)
    if not want:
        return False
    kind, *pairs = want.split(":")
    return kind == event_kind and all(str(facts.get(k)) == v for k, v in (p.split("=", 1) for p in pairs))


class HarnessMoved(RuntimeError):
    """A resume on a pin other than the one the run opened on."""

    def __init__(self, run_id, opened_on, now_on):
        super().__init__(f"run {run_id} opened on harness {opened_on} and this process runs {now_on}; "
                         f"resume on the same pin, or pass --migrate-harness to journal the move and owe every receipt")
        self.run_id, self.opened_on, self.now_on = run_id, opened_on, now_on


def runs_dir(c=None):
    d = state_dir(c) / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def slugify(text, limit=40):
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return (s or "run")[:limit]


def new_run_id(kind, ref):
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{slugify(ref, 24)}"


class Journal:
    def __init__(self, run_id, c=None):
        self.run_id = run_id
        self.dir = runs_dir(c) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "journal.jsonl"
        self.phase_path = self.dir / "phase.json"

    # ── events ────────────────────────────────────────────────────────

    def append(self, event_kind, **facts):
        ev = {"ts": now_iso(), "run_id": self.run_id, "v": VERSION, **facts, "kind": event_kind}
        if dies_at(event_kind, facts):
            die(event_kind)
        # drive shards run as threads over one journal; a line is written whole or not at all
        with _APPEND_LOCK, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return ev

    def events(self):
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"kind": "unparseable", "raw": line[:200]})
        return out

    # ── phase ─────────────────────────────────────────────────────────

    def open_run(self, beat, migrate=False, **facts):
        """The `opened` phase, bound to the harness version this process runs. A run that
        opened on another pin refuses (HarnessMoved) unless `migrate`, which journals the
        move; the reducer owes every receipt taken before it."""
        # the page asks this process whether the run is alive, never the journal's clock
        liveness.record(self.dir)
        now_on = versions.harness_version()
        was = self.fact("harness")
        if was and was != now_on:
            if not migrate:
                raise HarnessMoved(self.run_id, was, now_on)
            self.append("harness-migrated", opened_on=was, now_on=now_on)
        return self.phase("opened", beat=beat, harness=now_on, **facts)

    def phase(self, name, **facts):
        # a pause is honored at every phase the run moves into, not only before a seat
        from hb.record import control
        if name not in FINAL and name != STANDING and control.pause_requested(self.dir):
            self.append("control", verb="pause", before=name)
            raise control.Paused(self)
        self.append("phase", phase=name, **facts)
        return self._write_view()

    def record(self, **facts):
        """Add facts to the phase view without moving the phase (the evidence branch a PR links)."""
        self.append("record", **facts)
        return self._write_view()

    def read_phase(self):
        """The phase view, folded from the phase and record events: the last phase name and
        every fact the events carried, later ones winning."""
        cur = {}
        for ev in self.events():
            if ev.get("kind") == "phase":
                cur.update({k: v for k, v in ev.items() if k not in ("kind", "v", "run_id")})
            elif ev.get("kind") == "record":
                cur.update({k: v for k, v in ev.items() if k not in ("kind", "v", "run_id", "ts")})
        if cur:
            cur["run_id"] = self.run_id
        return cur

    def _write_view(self):
        cur = self.read_phase()
        atomic_write(self.phase_path, json.dumps(cur, indent=2, ensure_ascii=False, default=str))
        return cur

    def fact(self, key, default=None):
        return self.read_phase().get(key, default)

    # ── the loop's own effects ────────────────────────────────────────

    def intent(self, op_id, **facts):
        """The record before an effect of the loop's own (a fix seat, a merge, a receipt)."""
        return self.append("intent", op=op_id, **facts)

    def done(self, op_id, **facts):
        """The record after that effect; an intent with no done is reconciled on replay."""
        return self.append("done", op=op_id, **facts)

    # ── idempotent operations ─────────────────────────────────────────

    def op_state(self, key):
        """('complete', result) | ('started', facts) | (None, None)."""
        state, payload = None, None
        for ev in self.events():
            if ev.get("kind") == "op" and ev.get("key") == key:
                if ev.get("state") == "complete":
                    state, payload = "complete", ev.get("result")
                elif ev.get("state") == "started" and state != "complete":
                    state, payload = "started", ev.get("facts")
        return state, payload

    def fresh_key(self, base, landed):
        """The key to run `base` under on this pass: `base` itself, or `base-N` past
        every completed record whose result `landed()` rejects. A refused venue or a PR
        that never opened is a failed effect, and a resume must try it again rather than
        hand back the old refusal; a landed one is never repeated."""
        key, n = base, 1
        while True:
            state, payload = self.op_state(key)
            if state == "complete" and not landed(payload):
                n += 1
                # `.retryN`, never `-N`: the second drive's venue is keyed `venue-up-2` by
                # its stage, and a `-2` retry of `venue-up` collided with it

                key = f"{base}.retry{n}"
                continue
            return key

    def op(self, key, do, check=None, **facts):
        """Run `do()` once under `key`. On a resume after a crash, `check()` may
        return the effect's result if the effect already landed; None means it did
        not and `do()` runs. The result must be JSON-serialisable."""
        state, payload = self.op_state(key)
        if state == "complete":
            return payload
        if state == "started" and check is not None:
            found = check()
            if found is not None:
                self.append("op", key=key, state="complete", result=found, recovered=True)
                return found
        self.append("op", key=key, state="started", facts=facts)
        result = do()
        self.append("op", key=key, state="complete", result=result)
        return result

    # ── seats ─────────────────────────────────────────────────────────

    def next_attempt(self, seat):
        n = 0
        for ev in self.events():
            if ev.get("kind") == "seat" and ev.get("seat") == seat:
                n = max(n, int(ev.get("attempt") or 0))
        return n + 1

    def seat_dir(self, seat, attempt):
        d = self.dir / "seats" / f"{seat}-{attempt}"
        d.mkdir(parents=True, exist_ok=True)
        return d
