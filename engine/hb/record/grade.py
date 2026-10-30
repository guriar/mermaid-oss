"""hb.record.grade - the three numbers that grade a run, read from its journal alone.

Model minutes over wall minutes says how much of the run a model was working (the rest is
the harness waiting, admitting, measuring). Every instrument that fired, with what came of
it, says whether the harness was right to step in. Crashes and hand touches say how much of
the run was patchwork. Nothing here is a judgment; it is the three counts the owner reads
before deciding whether a change to the harness paid.

    hb run grade <run_id>
"""
import json
import re
from datetime import datetime
from pathlib import Path

from hb.rig.common import out
from hb.record.journal import FINAL, runs_dir

HAND_TOUCH_KINDS = {"harness-migrated": "the run was moved to another harness version",
                    }


def _ts(e):
    try:
        return datetime.fromisoformat(str(e.get("ts")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _minutes(a, b):
    return round((b - a).total_seconds() / 60, 1) if a and b else None


def read(journal_path: Path, launcher_log: Path = None):
    """The grade of one journal as a dict; `launcher_log` is the parent's log of this run's
    process when it has one (a wave keeps slice-<n>.out), where a crash's traceback lives."""
    events = [json.loads(l) for l in journal_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not events:
        return {"verdict": "empty-journal", "path": str(journal_path)}
    first, last = _ts(events[0]), _ts(events[-1])
    wall = _minutes(first, last)
    seats = [e for e in events if e.get("kind") == "seat" and e.get("minutes") is not None]
    model = round(sum(float(e["minutes"]) for e in seats), 1)
    phases = [e for e in events if e.get("kind") == "phase"]
    final = next((e for e in reversed(phases) if e.get("phase") in FINAL), None)

    instruments = []
    for e in events:
        k = e.get("kind")
        if k == "relaunched":
            after = next((s for s in seats if s.get("seat") == e.get("seat") and str(s.get("attempt")) != str(e.get("attempt"))
                          and _ts(s) and _ts(e) and _ts(s) > _ts(e)), None)
            instruments.append({"instrument": f"{e.get('seat')} relaunched ({e.get('why')})", "at": e.get("ts"),
                                "outcome": f"attempt {after.get('attempt')} {after.get('state')} in {after.get('minutes')} min"
                                if after else "no later attempt in the journal"})
        elif k == "seat" and e.get("seat") == "codex" and e.get("state") == "ok":
            instruments.append({"instrument": "second reader", "at": e.get("ts"), "outcome": f"read in {e.get('minutes')} min"})
        elif k == "phase" and e.get("phase") == "review" and e.get("round") is not None:
            instruments.append({"instrument": f"review round {e.get('round')}", "at": e.get("ts"),
                                "outcome": f"{e.get('findings')} findings, {e.get('fix')} to fix"})
        elif k == "fix-round":
            instruments.append({"instrument": f"fix round ({e.get('stage')})", "at": e.get("ts"), "outcome": str(e.get("result"))})
        elif k == "admission" and e.get("state") == "granted" and float(e.get("waited_s") or 0) >= 60:
            instruments.append({"instrument": f"admission held {e.get('workload')}", "at": e.get("ts"),
                                "outcome": f"waited {round(float(e['waited_s']))} s"})
        elif k == "stop":
            instruments.append({"instrument": f"stop ({e.get('stop_kind') or e.get('id')})", "at": e.get("ts"),
                                "outcome": ("answered: " if e.get("answered") else "open: ") + str(e.get("text"))[:120]})

    by_workload = {}
    for e in events:
        if e.get("kind") == "admission" and e.get("state") == "granted":
            k = str(e.get("workload") or "?").split(":")[0]
            by_workload[k] = by_workload.get(k, 0.0) + float(e.get("waited_s") or 0)
    wait_min = round(sum(by_workload.values()) / 60, 1)
    top_waits = [{"workload": k, "minutes": round(v / 60, 1)} for k, v in sorted(by_workload.items(), key=lambda kv: -kv[1])[:3]]

    classified = {"relaunched", "seat", "phase", "fix-round", "admission", "stop", "control"}
    other_kinds = sorted({str(e.get("kind")) for e in events} - classified)

    hand = []
    opened = [e for e in phases if e.get("phase") == "opened"]
    for e in opened[1:]:
        hand.append({"touch": "resumed by hand", "at": e.get("ts"), "minute": _minutes(first, _ts(e))})
    for e in events:
        if e.get("kind") == "control" and e.get("verb") in ("answer", "pause", "cut"):
            hand.append({"touch": f"{e.get('verb')} typed for the run", "at": e.get("ts"), "minute": _minutes(first, _ts(e))})
        if e.get("kind") in HAND_TOUCH_KINDS:
            hand.append({"touch": HAND_TOUCH_KINDS[e["kind"]], "at": e.get("ts"), "minute": _minutes(first, _ts(e))})
        if e.get("kind") == "seat" and e.get("state") in ("killed", "cut"):
            hand.append({"touch": f"{e.get('seat')} seat {e.get('state')}", "at": e.get("ts"), "minute": _minutes(first, _ts(e))})

    crashes = []
    if not final:
        running = [e for e in events if e.get("kind") == "op" and e.get("state") == "started"]
        done = {e.get("key") for e in events if e.get("kind") == "op" and e.get("state") == "complete"}
        stage = next((e.get("key") for e in reversed(running) if e.get("key") not in done), None) or (phases[-1].get("phase") if phases else None)
        frame = None
        if launcher_log and launcher_log.exists():
            text = launcher_log.read_text(encoding="utf-8", errors="replace")
            frames = re.findall(r'^  File "([^"]+)", line (\d+), in (\w+)\n    ([^\n]+)\n(\w+(?:Error|Exception)[^\n]*)', text, re.M)
            if frames:
                f = frames[-1]
                frame = {"file": Path(f[0]).name, "line": int(f[1]), "in": f[2], "error": f[4]}
        crashes.append({"ended_without": "a final phase", "last_event": events[-1].get("kind"), "at": events[-1].get("ts"),
                        "stage": stage, "model_minutes_before": model, **({"traceback": frame} if frame else {})})
    return {"run_id": events[0].get("run_id"), "wall_min": wall, "model_min": model,
            "model_share": round(model / wall, 2) if wall else None, "seats": len(seats),
            "wait_min": wait_min, "top_waits": top_waits,
            "instruments": instruments, "crashes": crashes, "hand_touches": hand, "other_event_kinds": other_kinds,
            "final": final.get("phase") if final else None}


def render(g):
    lines = [f"run {g.get('run_id')}",
             f"  model {g.get('model_min')} of {g.get('wall_min')} wall minutes"
             + (f" ({int(g['model_share'] * 100)}%)" if g.get("model_share") is not None else ""),
             f"  instruments fired: {len(g.get('instruments') or [])}"]
    lines += [f"    - {i['instrument']}: {i['outcome']}" for i in g.get("instruments") or []]
    lines.append(f"  crashes: {len(g.get('crashes') or [])}")
    for c in g.get("crashes") or []:
        tb = c.get("traceback")
        lines.append(f"    - ended without {c['ended_without']} in {c.get('stage')} after {c.get('model_minutes_before')} model minutes"
                     + (f"; {tb['error']} at {tb['file']}:{tb['line']} in {tb['in']}" if tb else ""))
    lines.append(f"  hand touches: {len(g.get('hand_touches') or [])}")
    lines += [f"    - minute {h.get('minute')}: {h['touch']}" for h in g.get("hand_touches") or []]
    return "\n".join(lines)


def launcher_log_for(run_dir: Path):
    """A wave keeps each slice process's output beside its own journal as slice-<n>.out."""
    try:
        first = json.loads(next(l for l in (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()))
    except (OSError, StopIteration, ValueError):
        return None
    wave, ref = first.get("wave"), str(first.get("ref") or "")
    if not wave or not ref:
        return None
    n = ref.lstrip("#")
    for p in sorted(run_dir.parent.glob(f"*-wave-{wave}/slice-{n}.out"), reverse=True):
        return p
    return None


def cmd_run_grade(a):
    run_dir = runs_dir() / a.ref
    j = run_dir / "journal.jsonl"
    if not j.exists():
        out({"verdict": "no-journal", "why": f"no journal at {j}"}, 2)
    g = read(j, launcher_log_for(run_dir))
    print(render(g))
    out({"verdict": "graded", **g}, 0)
