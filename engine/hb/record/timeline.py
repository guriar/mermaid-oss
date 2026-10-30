"""hb.run.timeline - a run's line and its steps, as the run page's timeline draws them.

The journal says when each seat and each round of checks starts and ends. This walks it
into stations on the run's line and into steps: who worked, when, for how long, what it
cost, and what it produced, read from the seat's own report.json. A report is read again
only when its file changes. It writes nothing; a file it cannot read leaves a field out.
"""
import json
import re
from pathlib import Path

from hb.rig.common import report_text
from hb.record.live import redact
from hb.runner.usage import label_role as _role, role_name
from hb.record.journal import LANDED

_REPORTS = {}
NOTE = 180
CHECK_WORDS = {"compose": "Checks on the combined wave", "build": "Checks after the build", "build-fix": "Checks after the check fixes", "suite": "Full test suite",
               "suite-fix": "Full test suite again", "ci-parity": "CI's own checks",
               "full": "Full test suite"}


def _short(text, limit=NOTE):
    s = " ".join(redact(text).split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def report(run_dir, label, attempt):
    """A seat's report.json, kept while the file is unchanged; None when it has none."""
    p = Path(run_dir) / "seats" / f"{label}-{attempt}" / "report.json"
    try:
        stamp = p.stat().st_mtime_ns
    except OSError:
        return None
    hit = _REPORTS.get(str(p))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        data = json.loads(report_text(p))
    except (OSError, ValueError):
        data = None
    _REPORTS[str(p)] = (stamp, data if isinstance(data, dict) else None)
    return _REPORTS[str(p)][1]


def who(label):
    label = str(label or "")
    name = role_name(label)
    if "." not in label and _role(label) == "merge" and "-" in label:
        name += " for " + label.split("-", 1)[1]
    return name


def walk(evs):
    """Where a hotfix or slice is on its line, the minutes each station took, the lanes of
    the station where the reviewer and the testers work at once, and the station each event
    left the run at. The run only moves forward along its line."""
    at, built, reviewed, fixed = 0.0, False, False, False
    minutes, lanes, where = {}, {}, []
    for e in evs:
        k = e.get("kind")
        if k == "seat":
            seat, role = str(e.get("seat")), _role(e.get("seat"))
            if e.get("state") == "started":
                if role == "build" and not built:
                    at = max(at, 1.4)
                elif role in ("review", "codex", "discovery"):
                    at = max(at, 5.4 if fixed else 3.4)
                elif role in ("verify", "audit"):
                    at = max(at, 6.4 if fixed else (3.4 if built else 0.4))
                elif role == "fix":
                    at = max(at, 4.4 if reviewed else 2.4)
                if int(at) == 3 and role != "codex":
                    lanes[seat] = {"name": who(seat),
                                   "p": 0.5, "state": ""}
            else:
                minutes[int(at)] = round(minutes.get(int(at), 0) + (e.get("minutes") or 0), 1)
                if seat in lanes and int(at) == 3:
                    lanes[seat]["p"] = 1 if e.get("state") == "ok" else lanes[seat]["p"]
                if role == "review" and e.get("state") == "ok":
                    reviewed = True
        elif k == "phase":
            ph = e.get("phase")
            if ph == "build":
                built, at = True, max(at, 2.4)
            elif ph == "gates":
                at = max(at, 6.9)
            elif ph in LANDED:
                at = 8.0
        elif k == "fix-round" and str(e.get("stage", "")).startswith("repair"):
            fixed, at = True, max(at, 5.0)
        elif k == "seat-kept":
            role = _role(e.get("seat"))
            built = built or role == "build"
            reviewed = reviewed or role == "review"
        where.append(int(at))
    return at, minutes, list(lanes.values()), where


def _wave_station(role, checked):
    return {"slice": 0, "merge": 1, "combine": 1, "verify": 3, "checks": 4, "pr": 5}.get(role, 3 if checked else 2)


ART_STAMP = re.compile(r"gate-(\d{8}T\d{6})Z")


def _stamp_s(text):
    from datetime import datetime, timezone
    m = re.search(r"(\d{4})-?(\d\d)-?(\d\d)T(\d\d):?(\d\d):?(\d\d)", str(text or ""))
    return datetime(*map(int, m.groups()), tzinfo=timezone.utc).timestamp() if m else None


def gate_seconds(rows, ended=None):
    """Each gate's own seconds: the row's `seconds`, else, for a journal from before rows
    carried a clock (gates then ran one at a time), from its artifact's start to the next
    gate's start or the round's end. None where neither says; nothing is guessed."""
    rows = [r for r in rows or [] if isinstance(r, dict)]
    found = [ART_STAMP.search(str(r.get("artifact") or "")) for r in rows]
    starts = [_stamp_s(m.group(1)) if m else None for m in found]
    out = []
    for i, r in enumerate(rows):
        if isinstance(r.get("seconds"), (int, float)):
            out.append(r["seconds"])
            continue
        nxt = starts[i + 1] if i + 1 < len(rows) else _stamp_s(ended)
        out.append(max(0.0, nxt - starts[i]) if starts[i] is not None and nxt is not None else None)
    return out


def _gate_detail(rows, ended=None):
    rows = [r for r in rows or [] if isinstance(r, dict)]
    secs = gate_seconds(rows, ended)
    out = [{"name": str(r.get("name")), "state": "pass" if r.get("true_exit") == 0 else "blank" if r.get("true_exit") is None else "red",
            **({"seconds": secs[i]} if secs[i] is not None else {})}
           for i, r in enumerate(rows)]
    red = [r["name"] for r in out if r["state"] == "red"]
    blank = [r["name"] for r in out if r["state"] == "blank"]
    words = f"{len(out) - len(red) - len(blank)} passed" + (f", {len(red)} red" if red else "") + (f", {len(blank)} measured nothing" if blank else "")
    return words, {"checks": out, "red": red, "blank": blank}, "red" if red or blank else "ok"


def _seat_detail(run_dir, label, attempt):
    """(one line, detail) from a seat's report: what it built, found, fixed or saw."""
    rep = report(run_dir, label, attempt) or {}
    role = _role(label)
    if role in ("build", "fix") and rep and "items" in rep:
        # a fix round is the builder relaunched, so it writes a build note (A16)
        items = [{"item": i.get("item"), "status": str(i.get("status") or ""), "note": _short(i.get("because") or "", 160)}
                 for i in rep.get("items") or [] if isinstance(i, dict)]
        other = [i for i in items if i["status"] != "built"]
        words = f"{len(items)} items built" + (f", {len(other)} differently" if other else "")
        return words, {"changelog": _short(rep.get("changelog") or "", 300),
                       "for_user": [_short(x, 260) for x in rep.get("for_user") or []],
                       "items": items, "decisions": [_short((d or {}).get("text") if isinstance(d, dict) else d) for d in (rep.get("decisions") or [])[:8]],
                       "decisions_total": len(rep.get("decisions") or []), "files": len(rep.get("files") or []), "tests": len(rep.get("tests") or [])}
    if role == "review" and rep:
        fs = [{"id": f.get("id"), "severity": f.get("severity"), "title": _short(f.get("title") or f.get("summary"), 200),
               "status": f.get("status") or "open"} for f in rep.get("findings") or [] if isinstance(f, dict)]
        gv = rep.get("goal_verdict") or {}
        still = sum(1 for f in fs if f["status"] == "open")
        words = (f"{len(fs)} finding{'s' if len(fs) != 1 else ''}" + (f", {still} open" if still and still != len(fs) else "")
                 + ("" if gv.get("met", True) else ", goal not met yet")) if fs else "No findings"
        if fs and not still:
            words = f"{len(fs)} findings, all closed"
        return words, {"findings": fs, "goal_met": gv.get("met"), "misses": [_short(m, 240) for m in gv.get("misses") or []],
                       "scope": rep.get("scope"), "codex": rep.get("codex")}
    if role == "fix" and rep:
        # reports on disk from runs before the builder fixed its own reds listed fixes
        fixes = [{"finding": _short(f.get("finding"), 120), "change": _short(f.get("change"), 260)}
                  for f in rep.get("fixes") or [] if isinstance(f, dict)]
        left = [_short(x if isinstance(x, str) else json.dumps(x), 200) for x in rep.get("left_open") or []]
        return f"{len(fixes)} fix{'es' if len(fixes) != 1 else ''}" + (f", {len(left)} left open" if left else ""), {"fixes": fixes, "left_open": left}
    if role == "discovery" and rep:
        finds = [{"item": f.get("item"), "said": _short(f.get("said"), 200), "wrong": _short(f.get("wrong"), 220)}
                 for f in rep.get("finds") or [] if isinstance(f, dict)]
        return (f"{len(finds)} find{'s' if len(finds) != 1 else ''}" if finds else "Nothing new"), {"finds": finds}
    if role in ("verify", "audit") and rep:
        rows = [{"item": r.get("item"), "state": r.get("state") or "", "seen": _short(r.get("seen") or r.get("observed") or "", 220)}
                for r in rep.get("items") or [] if isinstance(r, dict) and r.get("substate") != "before-capture"]
        n = lambda s: sum(1 for r in rows if r["state"] == s)
        return f"{n('WORKS')} work, {n('BROKEN')} broken, {n('UNMEASURED')} not measured", {"items": rows}
    return "", {}


def collect(run_dir, evs, kind="slice", running=False):
    """Every step of the run, in start order, each with a lane so steps at the same time sit
    side by side. A seat that never ended is still working while the run is, and was cut
    short when the run started again or stopped."""
    _, _, _, where = walk(evs)
    steps, open_, checks_open = [], {}, {}
    checked = False

    def station(i, role):
        return _wave_station(role, checked) if kind == "wave" else where[i]

    for i, e in enumerate(evs):
        k = e.get("kind")
        if k == "phase" and e.get("phase") == "check":
            checked = True
        if k == "seat":
            key = (str(e.get("seat")), e.get("attempt") or 1)
            if e.get("state") == "started":
                s = {"id": f"{key[0]}-{key[1]}", "kind": "seat", "role": _role(key[0]), "name": who(key[0]), "attempt": key[1],
                     "station": station(i, _role(key[0])), "start": e.get("ts"), "end": None, "state": "running",
                     "model": e.get("model"), "summary": "", "detail": {}, "questions": []}
                open_[key] = s
                steps.append(s)
            elif key in open_:
                s = open_.pop(key)
                u = e.get("usage") or {}
                s.update(end=e.get("ts"), state="ok" if e.get("state") == "ok" else "failed", minutes=e.get("minutes"),
                         usd=u.get("cost_usd") if isinstance(u, dict) else None,
                         tokens=(u.get("output_tokens") or u.get("total_tokens")) if isinstance(u, dict) else None,
                         model=" ".join(str(x) for x in (e.get("model"), e.get("effort")) if x) or s["model"])
                if e.get("words"):
                    s["summary"] = str(e["words"])  # a paused second reader, in the owner's words
                elif e.get("state") == "context":
                    # a builder whose memory filled is handed off, not failed: the next one reads its notes
                    s["state"] = "handed"
                    s["summary"] = f"Ran out of room after {e.get('minutes') or 0} min: its memory filled. The next builder took over from its notes."
                elif e.get("state") != "ok":
                    s["summary"] = f"Ended: {e.get('state')}" + (f", {_short((e.get('problems') or [''])[0], 140)}" if e.get("problems") else "")
                else:
                    s["summary"], s["detail"] = _seat_detail(run_dir, key[0], key[1])
        elif k == "question" and e.get("seat"):
            for s in reversed(steps):
                if s["kind"] == "seat" and s["id"] == f"{e.get('seat')}-{e.get('attempt') or 1}":
                    s["questions"].append(_short(e.get("text"), 400))
                    break
        elif k == "op" and str(e.get("key", "")).startswith("gates-"):
            stage = str(e.get("key")).split("@")[0][len("gates-"):]
            if e.get("state") == "started":
                combine = stage == "compose" or stage.startswith("merge-")
                name = CHECK_WORDS.get(stage) or ("Checks after repair " + stage.split("-")[-1] if stage.startswith("repair")
                                                  else f"Checks after merging #{stage.split('-')[-1]}" if stage.startswith("merge-")
                                                  else f"Checks: {stage}")
                checks_open[stage] = s = {"id": f"checks-{i}", "kind": "checks", "role": "checks", "name": name,
                                          "station": station(i, "combine" if combine else "checks"), "start": e.get("ts"),
                                          "end": None, "state": "running", "summary": "", "detail": {}}
                steps.append(s)
            elif e.get("state") == "complete" and stage in checks_open:
                s = checks_open.pop(stage)
                s["summary"], s["detail"], s["state"] = _gate_detail(e.get("result"), e.get("ts"))
                s["end"] = e.get("ts")
        elif k == "op" and e.get("key") == "pr" and e.get("state") == "complete":
            url = (e.get("result") or {}).get("url") if isinstance(e.get("result"), dict) else None
            steps.append({"id": "pr", "kind": "pr", "role": "pr", "name": "PR opened" if url else "PR did not open",
                          "station": station(i, "pr") if kind == "wave" else 7,
                          "start": e.get("ts"), "end": e.get("ts"), "state": "ok" if url else "failed",
                          "summary": url or _short((e.get("result") or {}).get("error") if isinstance(e.get("result"), dict) else ""),
                          "detail": {"url": url}})
        elif k == "stop":
            steps.append({"id": f"stop-{e.get('id') or i}", "kind": "stop", "role": "stop", "name": "Waiting on you",
                          "station": station(i, "stop"), "start": e.get("ts"), "end": None, "state": "waiting",
                          "summary": _short(e.get("text"), 200), "detail": {"text": _short(e.get("text"), 600)}})
        elif k == "slice" and e.get("state") == "started":
            steps.append({"id": f"slice-{e.get('issue')}", "kind": "slice", "role": "slice", "name": f"Slice #{e.get('issue')}",
                          "station": 0, "start": e.get("ts"), "end": None, "state": "running", "summary": "", "detail": {"issue": str(e.get("issue"))}})
        elif k in ("compose-merge", "compose-park"):
            for s in steps:
                if s["kind"] == "slice" and s["detail"].get("issue") == str(e.get("issue")) and not s["end"]:
                    s["end"], s["state"] = e.get("ts"), "ok" if k == "compose-merge" else "failed"
                    s["summary"] = "Merged into the wave" if k == "compose-merge" else _short("Left out: " + str(e.get("why")))
        elif k == "phase" and e.get("phase") == "opened":
            # a stop is answered and a seat cut short when the run starts again
            for s in steps:
                if not s["end"] and s["kind"] in ("stop", "seat", "checks"):
                    s["end"] = e.get("ts")
                    if s["kind"] == "stop":
                        s["state"] = "answered"
                    elif s["kind"] == "seat" and report(run_dir, *s["id"].rsplit("-", 1)):
                        # the seat wrote its note; the harness died before reading it
                        line, s["detail"] = _seat_detail(run_dir, *s["id"].rsplit("-", 1))
                        s["state"], s["summary"] = "ok", f"{line.rstrip('.')}. The harness crashed right after, and the run started again."
                    else:
                        s["state"], s["summary"] = "cut", "Cut short: the harness crashed or was stopped, and the run started again"
                    open_ = {key: v for key, v in open_.items() if v is not s}
                    checks_open = {key: v for key, v in checks_open.items() if v is not s}
    last = evs[-1].get("ts") if evs else None
    for s in steps:
        if not s["end"] and not running and s["state"] in ("running", "waiting") and s["kind"] != "stop":
            s["end"], s["state"], s["summary"] = last, "cut", s["summary"] or "Did not finish: the run ended first"
    _second_reader(steps)
    return _lanes(steps)


def _second_reader(steps):
    """The second reader writes no report; the review that follows it says what it kept."""
    for n, s in enumerate(steps):
        if s["role"] == "codex" and s["state"] == "ok":
            nxt = next((r for r in steps[n + 1:] if r["role"] == "review" and (r["detail"] or {}).get("codex")), None)
            c = (nxt or {}).get("detail", {}).get("codex") or {}
            if c.get("read"):
                s["summary"] = f"{c.get('adopted') or 0} points kept by the reviewer, {c.get('refuted') or 0} set aside"
                s["detail"] = {"adopted": c.get("adopted") or 0, "refuted": c.get("refuted") or 0, "reviewer": nxt["id"],
                               "claims": [k for k in c.get("claims") or [] if isinstance(k, dict)]}


def _lanes(steps):
    """Give each step the first lane free at its start, so parallel work fans out."""
    ends = []
    for s in steps:
        for n, end in enumerate(ends):
            if end is not None and end <= (s["start"] or ""):
                s["lane"] = n
                ends[n] = s["end"]
                break
        else:
            s["lane"] = len(ends)
            ends.append(s["end"])
    return steps
