"""hb.run.view - a run as the owner sees it, read from its journal.

One reader for the run page and the board: where a run is on its line, which lanes are
working side by side, what each item reads, what waits on the owner, and what it has
cost. It writes nothing and decides nothing; every fact comes from the journal and the
files the run already left.
"""
import functools
import json
import time
from pathlib import Path

from hb.rig.common import report_text
from hb.record import control
from hb.record import live
from hb.runner import liveness
from hb.record import timeline
from hb.runner import usage
from hb.record import reducer
from hb.record.journal import runs_dir
from hb.record.prbody import wall_minutes

from hb.record.journal import FINAL, FINISHED, HALTED, LANDED, PAUSED_LIMIT, STANDING, STOPPED
STANDING_WORDS = "standing: PR open, waiting for the chat or the merge"
RUN_LINE = ["Before", "Build", "Checks", "Review + test", "Fix", "Closing check", "Retest", "PR"]
WAVE_LINE = ["Slices", "Combine", "Whole review", "Seams", "Full suite", "PR"]
WAVE_AT = {"queue": 0.2, "wave-branch": 0.2, "slices": 0.5, "compose": 1.5, "review": 2.5, "check": 3.2,
           "drive": 3.6, "repair": 3.6, "gates": 4.5}
_CACHE = {}


@functools.lru_cache(maxsize=1)
def root():
    """The runs folder, found once: finding it asks git, and the board asks for every run."""
    return runs_dir()


def _events(run_dir):
    p = Path(run_dir) / "journal.jsonl"
    try:
        st = p.stat()
    except OSError:
        return []
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _CACHE.get(str(p))
    if hit and hit[0] == stamp:
        return hit[1]
    evs = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            evs.append(json.loads(line))
        except ValueError:
            continue
    _CACHE[str(p)] = (stamp, evs)
    return evs


def _json(path):
    try:
        return json.loads(report_text(path))
    except (OSError, ValueError):
        return None


def _fact(evs, phase, key=None):
    for e in reversed(evs):
        if e.get("kind") == "phase" and e.get("phase") == phase:
            return e.get(key) if key else e
    return None


def _op(evs, key):
    for e in reversed(evs):
        if e.get("kind") == "op" and e.get("key") == key and e.get("state") == "complete":
            return e.get("result")
    return None


def _receipts(run_dir, evs, items):
    """The newest reading of each item: every drive's rows in the order they were written,
    then the rows a test proved."""
    rows = {}
    seats = Path(run_dir) / "seats"
    reports = sorted((d / "report.json" for d in seats.glob("verify-*")), key=lambda p: p.stat().st_mtime if p.exists() else 0) \
        if seats.exists() else []
    for rp in reports:
        for r in (_json(rp) or {}).get("items") or []:
            if r.get("substate") != "before-capture" and r.get("item"):
                rows[r["item"]] = r
    for e in evs:
        if e.get("kind") == "proven-by-test":
            for i in e.get("items") or []:
                rows[i] = {"item": i, "state": "WORKS", "substate": "by-test"}
    out = []
    for it in items:
        r = rows.get(it["item"]) or {}
        b = r.get("battery") or {}
        out.append({"item": it["item"], "text": it.get("then") or "", "proof": it.get("proof") or "eyes",
                    "rate": it.get("pass_rate"), "state": r.get("state") or "", "substate": r.get("substate") or "",
                    "pass_k": f"pass^k {b['pass_k']} over {b.get('cases')} cases" if b.get("pass_k") is not None else "",
                    "seen": r.get("seen") or ""})
    return out


def _changelog(run_dir):
    seats = Path(run_dir) / "seats"
    for rp in sorted(seats.glob("build-*/report.json"), reverse=True) if seats.exists() else []:
        line = str((_json(rp) or {}).get("changelog") or "").strip()
        if line:
            return line
    return ""


def _shots(run_dir, run_id):
    """Before and after pairs by item and viewport (`<item>-<viewport>.png`), as addresses the
    page can load."""
    ev = Path(run_dir) / "evidence"
    pairs = []
    for b in sorted((ev / "before").glob("*.png")) if (ev / "before").is_dir() else []:
        after = ev / "after" / b.name
        pairs.append({"item": b.stem, "before": f"/file/{run_id}/before/{b.name}",
                      "after": f"/file/{run_id}/after/{b.name}" if after.exists() else None})
    return pairs


def _demo(run_dir, run_id):
    """The film the demo seat made after the merge, as the page plays it: the video, the
    seat's own summary and chapters, and what launch.json says when no video came of it."""
    d = Path(run_dir) / "evidence" / "demo"
    if not d.is_dir():
        return None
    films = sorted(d.glob("*.webm")) + sorted(d.glob("*.mp4"))
    launch = _json(d / "launch.json") or {}
    text = lambda name: (d / name).read_text(encoding="utf-8", errors="replace") if (d / name).is_file() else ""
    if not films and not launch:
        return None
    return {"video": f"/file/{run_id}/demo/{films[0].name}" if films else None,
            "verdict": launch.get("verdict") or ("filmed" if films else None), "why": launch.get("why"),
            "summary": text("summary.md"), "chapters": text("chapters.md")}


def _open_stops(run_dir, evs, since="opened"):
    """The run's unanswered stops since its last `since` boundary: a pass's opening, or for a
    standing run its last next step, so a stop an earlier step outlived is not shown."""
    answered = {k.casefold() for k in control.answers_by_id(run_dir)}
    seen, out = set(), []
    last_open = max((i for i, e in enumerate(evs) if (e.get("kind"), e.get("phase")) == ("phase", "opened")
                     or (since == "next" and e.get("kind") == "next")), default=0)
    for e in evs[last_open:]:
        if e.get("kind") == "stop" and e.get("id") and e["id"] not in seen:
            seen.add(e["id"])
            if e["id"].casefold() not in answered:
                out.append({"id": e["id"], "kind": e.get("stop_kind"), "text": e.get("text")})
    return out


def _state(evs, run_dir=None):
    """The last final phase; else running while the run's process is there, however quiet,
    and ended once it is gone or never recorded."""
    last = next((e.get("phase") for e in reversed(evs) if e.get("kind") == "phase"), None)
    if last in FINAL:
        return last
    up = liveness.alive(run_dir) if run_dir else None
    if last == STANDING:
        # a standing run is live while its process is there: no seat, its PR open
        return "ended" if up is False else "standing"
    return "running" if up else "ended"


def _cost(evs, running=False):
    s = usage.spent(evs)
    passes = sum(1 for e in evs if e.get("kind") == "phase" and e.get("phase") == "opened")
    return {"agent_min": round(s["minutes"]), "list_usd": s["usd"], "steps": s["steps"],
            "wall_min": wall_minutes(evs, time.time() if running else None),
            "restarts": max(0, passes - 1), "kept": sum(1 for e in evs if e.get("kind") == "seat-kept"),
            "peak_context_tokens": s["peak_context_tokens"]}


def _slices(run_id, evs):
    """A wave's slices, each read from its own run: the newest slice run that names this wave."""
    wave = _fact(evs, "opened", "ref") or ""
    issues = [str(e.get("issue")) for e in evs if e.get("kind") == "slice" and e.get("state") == "started"]
    newest = {}
    for d in sorted(root().iterdir()):
        if "-slice-" not in d.name or d.name < run_id[:15] or d.name[:8] > _days_after(run_id[:8], 5):
            continue
        opened = _fact(_events(d), "opened") or {}
        ref = str(opened.get("ref") or "").lstrip("#")
        if opened.get("wave") and ref in issues and str(opened.get("wave")) in (wave, wave.split("/")[-1]):
            newest[ref] = d.name
    rows = []
    for n in dict.fromkeys(issues):
        merged = any(e.get("kind") == "compose-merge" and str(e.get("issue")) == n for e in evs)
        parked = next((e.get("why") for e in evs if e.get("kind") in ("compose-park",) and str(e.get("issue")) == n), None)
        sub = summary(newest[n]) if n in newest else {}
        rows.append({"issue": n, "run_id": newest.get(n), "title": sub.get("title") or f"Slice {n}",
                     "p": 1 if merged else min(0.95, (sub.get("at") or 0) / len(RUN_LINE)),
                     "state": "halt" if sub.get("needs") or parked else "", "merged": merged, "parked": parked,
                     "works": sub.get("works"), "total": sub.get("total"), "where": "merged into the wave" if merged else sub.get("where"),
                     "needs": sub.get("needs") or 0})
    return rows


def _days_after(yyyymmdd, days):
    try:
        t = time.mktime(time.strptime(yyyymmdd, "%Y%m%d")) + days * 86400
    except ValueError:
        return "99999999"
    return time.strftime("%Y%m%d", time.localtime(t))


def run(run_id, detail=True):
    """Everything the run page shows for one run. Without `detail` the working seats are
    not read: the board needs only the run's line."""
    d = root() / run_id
    evs = _events(d)
    if not evs:
        return None
    opened = _fact(evs, "opened") or {}
    kind = opened.get("beat") or "hotfix"
    contract = _op(evs, "contract") or {}
    items = contract.get("items") or []
    state = _state(evs, d)
    stops = _open_stops(d, evs, "next" if state == STANDING else "opened") if state in (STOPPED, "paused", STANDING) else []
    final = _fact(evs, state) if state in FINAL or state == STANDING else {}
    view = {"id": run_id, "kind": kind, "wave": opened.get("wave"), "ref": opened.get("ref"),
            "title": (_changelog(d) if kind != "wave" else "") or contract.get("headline") or contract.get("goal")
                     or opened.get("ref") or run_id,
            "shots": _shots(d, run_id),
            "issue": contract.get("issue"), "started": evs[0].get("ts"), "last": evs[-1].get("ts"), "state": state,
            "pr": (final or {}).get("pr"), "stops": stops, "cost": _cost(evs, state == "running"),
            "finish": reducer.finish_of(evs),
            "launch": next((e.get("card") for e in evs if e.get("kind") == "launch"), None),
            "decided": [e.get("text") for e in evs if e.get("kind") == "question"],
            "activity": live.activity(evs),
            "seats": live.seats(d, evs) if detail and state == "running" else [],
            "first_screen": _json(d / "evidence" / "first-screen.json") if state in LANDED else None,
            "closeout": _json(d / "evidence" / "closeout.json"),
            "demo": _demo(d, run_id),
            "second_reader": live.second_reader(evs)}
    if kind == "wave":
        last = next((e.get("phase") for e in reversed(evs) if e.get("kind") == "phase" and e.get("phase") in WAVE_AT), None)
        at = float(len(WAVE_LINE)) if state in FINISHED else WAVE_AT.get(last, 0.2)
        slices = _slices(run_id, evs)
        view.update(line=WAVE_LINE, fan=0, at=at, lanes=[{"name": f"{s['issue']} {s['title']}", "p": s["p"], "state": s["state"]}
                                                          for s in slices], slices=slices, minutes={})
        for s in slices if detail else []:
            sd = root() / s["run_id"] if s.get("run_id") and not s.get("merged") else None
            sevs = _events(sd) if sd else []
            if sevs and _state(sevs, sd) == "running":
                view["seats"] += [dict(p, role=f"Slice {s['issue']}, {p['role']}") for p in live.seats(sd, sevs)]
        view["stops"] = stops
        view["needs"] = len(stops) + sum(s["needs"] for s in slices)
    else:
        at, minutes, lanes, _ = timeline.walk(evs)
        line = RUN_LINE[:-1] + ["Merge into wave"] if opened.get("wave") else RUN_LINE
        view.update(line=line, fan=3, at=at, lanes=lanes or [{"name": "Reviewer", "p": 0, "state": ""}], minutes=minutes,
                    needs=len(stops))
    # a standing run halts only on a stop a step raised; otherwise it waits on the chat or the merge
    view["halt"] = state in HALTED or (state == STANDING and bool(stops))
    view["steps"] = timeline.collect(d, evs, kind, running=state == "running") if detail else []
    view["now"] = time.time()
    view["items"] = _receipts(d, evs, items)
    view["works"] = sum(1 for r in view["items"] if r["state"] == "WORKS")
    view["total"] = len(view["items"])
    if kind == "wave" and not view["total"]:
        view["works"] = sum(s["works"] or 0 for s in view["slices"])
        view["total"] = sum(s["total"] or 0 for s in view["slices"])
    if view["closeout"]:
        view["stops"], view["needs"], view["halt"] = [], 0, False
    i = min(int(view["at"]), len(view["line"]) - 1)
    view["where"] = "Merged and cleaned up" if view["closeout"] else STANDING_WORDS if state == "standing" \
        else "PR open" if view["pr"] else view["line"][i]
    if state == PAUSED_LIMIT:
        # nobody answers a usage limit: the card says when the plan is back, not that it needs you
        view["limit"] = (final or {}).get("resets") or "unknown"
        view["where"] = f"paused: usage limit, resets {view['limit']}"
    if view["closeout"]:
        view["state"] = "closed"
    return view


_SUMMARY = {}
ROW_KEYS = ("id", "kind", "wave", "ref", "title", "state", "where", "works", "total", "needs", "at", "pr", "started", "last", "cost")


def row_of(run_id):
    """The board's line for a run, read now: its summary, its own open questions and what
    its seats spent by day."""
    d = root() / run_id
    v = run(run_id, detail=False)
    if not v:
        return {}
    return {**{k: v[k] for k in ROW_KEYS}, "asks": len(v["stops"]), "usage": usage.of(d, _events(d)),
            "shipped": bool(v["closeout"]), "film": bool((v["demo"] or {}).get("video"))}


def summary(run_id):
    """The board's line for a run, kept while its journal and its process are unchanged."""
    d = root() / run_id
    try:
        st = (d / "journal.jsonl").stat()
    except OSError:
        return {}
    hit = _SUMMARY.get(run_id)
    up = liveness.alive(d)
    # the closeout and the film land after the journal's last line, so their folder is in the stamp
    after = [(d / "evidence" / n).stat().st_mtime_ns for n in ("closeout.json", "demo") if (d / "evidence" / n).exists()]
    stamp = (st.st_mtime_ns, st.st_size, up, tuple(after))
    # a run with no pid record reads its state from the clock, so it is read again each time
    if hit and hit[0] == stamp and (up is not None or hit[1].get("state") != "running"):
        return hit[1]
    row = row_of(run_id)
    if row:
        _SUMMARY[run_id] = (stamp, row)
    return row


HOME_FINISHED = 60


def card(run_id):
    """A live run as the home shows it: its seats, actions and questions; its step list is
    its own page's."""
    v = run(run_id)
    if v:
        v.pop("steps", None)
    return v


def placed(rows):
    """Which runs the home shows in full, newest first: every run whose process is there,
    and every run stopped on a question of its own that no later run of the same ref has
    taken over. A slice inside a wave shows inside its wave unless it waits on an answer."""
    out, later = [], set()
    for r in rows:
        key = (r.get("kind"), str(r.get("ref") or r["id"]))
        if (r["state"] in ("running", "standing") and not r.get("wave")) or (r.get("asks") and key not in later):
            out.append(r)
        later.add(key)
    return out


def compose(rows, cards, now=None):
    """The home from every run's line, newest first, and the cards of the placed ones."""
    top = [r for r in rows if not r.get("wave")]
    shown = placed(rows)
    ids = {r["id"] for r in shown}
    live_runs = [cards[r["id"]] for r in shown if cards.get(r["id"])]
    now = now or time.time()
    week_ago = time.strftime("%Y%m%d", time.gmtime(now - 7 * 86400))
    week = [s for s in rows if s["id"][:8] >= week_ago]
    return {"live": live_runs, "finished": [r for r in top if r["id"] not in ids][:HOME_FINISHED],
            "needs": sum(len(v.get("stops") or []) for v in live_runs), "usage": usage.meters(rows, now),
            "week": {"finished": sum(1 for s in week if s["state"] in LANDED and s["state"] != STANDING and not s.get("wave")),
                     "agent_min": sum(s["cost"]["agent_min"] for s in week),
                     "list_usd": round(sum(s["cost"]["list_usd"] for s in week))},
            "now": now}


def rows_all():
    names = sorted((d.name for d in root().iterdir() if d.is_dir()), reverse=True)
    return [s for s in (summary(n) for n in names) if s]


def home():
    """The owner's one address, read now. The page server serves the watcher's copy."""
    rows = rows_all()
    return compose(rows, {r["id"]: card(r["id"]) for r in placed(rows)})


