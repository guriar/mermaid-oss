"""hb.run.stops - every stop the runner records has a stable id, and an answer names it.

Ids: a review finding keeps its own (`F1`); an authority stop is `A-` plus a short hash of
its kind and text; a gate stop is `G-` plus a hash of the gate's name; a parked slice is
`P-<issue>`; a conductor's repair brief is `C-` plus a hash of its text; a usage limit is `L-`
plus a hash of the CLI's words (it parks the run, nobody answers it); any other stop is
`S-` plus a hash of its text. The conductor answers one with `answer: <id>: <text>` in
control.txt. On resume each door routes an answered id to its place:

  finding    to the fix seat, the answer beside the finding (policy.route_findings)
  authority  the build that raised it runs again with the answer (slice door)
  gate       the gate runs again (hotfix._gates keys the round on the answer)
  conductor  the first fix seat of the repair loop reads it (fixer_briefs)
  parked     the wave resumes the slice with the answer on each of its own stop ids

An answered stop is never raised again as unanswered: `raise_stop` refuses an answered
authority or conductor id, and a gate still red after its rerun says so in its text.
"""
import hashlib
import re

from hb.record import control

STOP_LINE = re.compile(r"^\[([^\]]+)\]\s")


def short_hash(*parts):
    return hashlib.sha1("\n".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:4]


def authority_id(na):
    return "A-" + short_hash(na.get("kind") or "", (na.get("text") or "").strip())


def gate_id(name):
    return "G-" + short_hash(name)


def parked_id(issue):
    return f"P-{str(issue).lstrip('#')}"


def conductor_id(text):
    return "C-" + short_hash(text.strip())


def limit_id(text):
    return "L-" + short_hash(text.strip())


def other_id(text):
    return "S-" + short_hash(text)


def line(sid, text):
    """The stop as the report, the phase file and the conductor read it: the id first."""
    return f"[{sid}] {text}"


def id_of(stop_line):
    m = STOP_LINE.match(str(stop_line or ""))
    return m.group(1) if m else None


def answers(j):
    """{id casefolded: answer text} from the run's control file."""
    return control.answers_by_id(j.dir)


def answer_for(j, sid):
    return answers(j).get(str(sid or "").casefold())


def raise_stop(j, stops, kind, sid, text):
    """Append the stop to `stops` as its id line and journal it. An answered authority or
    conductor id is routed, not raised: the door already rebuilt or briefed with the answer."""
    answered = answer_for(j, sid)
    j.append("stop", id=sid, stop_kind=kind, text=text, answered=bool(answered))
    if answered and kind in ("authority", "conductor"):
        return False
    stops.append(line(sid, text + (" (answered; red again after the rerun)" if answered and kind == "gate" else "")))
    return True


def authority(j, stops, note, where=""):
    """Each authority row of a builder's note raised as a stop, unless its id is answered."""
    for na in _authority_rows(note):
        raise_stop(j, stops, "authority", authority_id(na),
                   f"{na['kind']}: {na.get('text')} ({na.get('evidence')})" + (f" {where}" if where else ""))


def _authority_rows(note):
    from hb.contract.policy import authority_stops
    return authority_stops(note)


def ruled_authority(j, note):
    """{id: answer} for the authority rows of a note the conductor answered."""
    out = {}
    for na in _authority_rows(note):
        sid = authority_id(na)
        ans = answer_for(j, sid)
        if ans:
            out[sid] = {"stop": f"{na['kind']}: {na.get('text')}", "answer": ans}
    return out


def gates(j, stops, red, why):
    """One stop per red gate, each with its own id, so an answer can rerun exactly it."""
    for g in red:
        raise_stop(j, stops, "gate", gate_id(g["name"]), f"{why}: {g['name']}")


def record_conductor(j, text):
    """A repair brief is a stop the conductor raised and answered in one line: it names what
    no receipt and no finding holds (a red CI shard), and it goes to the first fixer."""
    sid = conductor_id(text)
    if not answer_for(j, sid):
        with open(control.path(j.dir), "a", encoding="utf-8") as f:
            f.write(f"answer: {sid}: {text.strip()}\n")
    j.append("stop", id=sid, stop_kind="conductor", text=text.strip(), answered=True)
    return sid


def routed(j):
    """The stop ids whose answer already reached the seat it routes to in this run."""
    return {e.get("id") for e in j.events() if e.get("kind") == "answer-routed" and e.get("id")}


def fixer_briefs(j):
    """Conductor stops recorded in this run and answered, that no fix seat has read yet."""
    done = routed(j)
    out, seen = [], set()
    for e in j.events():
        sid = e.get("id")
        if e.get("kind") == "stop" and e.get("stop_kind") == "conductor" and sid not in done and sid not in seen:
            ans = answer_for(j, sid)
            if ans:
                seen.add(sid)
                out.append({"id": sid, "answer": ans})
    return out


def mark_routed(j, entries, to):
    for e in entries or []:
        j.append("answer-routed", id=e["id"], to=to)


def open_lines(stops):
    """The report's "Yours to rule" rows for the stops, each with how to answer it."""
    rows = []
    for s in stops or []:
        sid = id_of(s)
        rows.append(f"- {s}" + (f"; answer with `answer: {sid}: <your ruling>` in control.txt" if sid else ""))
    return rows
