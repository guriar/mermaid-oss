"""hb.run.control - the conductor's control file, read by the runner at its boundaries.

The chat that launched a run is its conductor. It steers nothing about the work (the
builder is trusted), but it operates the run through one file, `control.txt` in the run
directory, one verb per line:

  pause                  finish the seat that is running, then stop; `--resume` re-enters
  cut                    kill the seat that is running now; the runner relaunches it from
                         its progress file, and the cut does not count as a failed attempt
  answer: <text>         an answer to a question a seat raised; it rides into the brief of
                         every seat that starts after it, and the PR body names which seat
                         reached it first, or that it arrived after every seat
  answer: <id>: <text>   the same, and it answers the stop with that id (see stops.py):
                         on resume the door routes it, and the stop is not raised again
  next: <verb> [text]    a standing run's next step (see standing.py); `cut` while it
                         stands ends it

`pause`, `cut` and `next` are consumed: the runner removes the line when it acts and
journals it. An `answer` line stays; the journal records the first seat it reached.
"""
import re
from pathlib import Path

from hb.rig.common import atomic_write

FILE = "control.txt"


class Paused(Exception):
    """Raised at a boundary when the control file says pause; the door records it."""

    def __init__(self, journal):
        super().__init__("paused")
        self.journal = journal


class UsageLimited(Paused):
    """Raised when a seat met the plan's usage limit: the run parks until the reset, and
    `--resume` re-enters at that seat."""

    def __init__(self, journal, seat, limit):
        super().__init__(journal)
        self.seat = seat
        self.text = limit.get("text") or "the plan's usage limit is reached"
        self.resets = limit.get("resets") or "unknown"
        self.resets_at = limit.get("resets_at")


def path(run_dir):
    return Path(run_dir) / FILE


def _lines(run_dir):
    p = path(run_dir)
    if not p.exists():
        return []
    try:
        return [l.rstrip("\r\n") for l in p.read_text(encoding="utf-8-sig").splitlines()]
    except OSError:
        return []


def _write(run_dir, lines):
    atomic_write(path(run_dir), "\n".join(lines) + ("\n" if lines else ""))


def take(run_dir, verb):
    """True when a `verb` line is present; the line is removed (consumed once)."""
    lines = _lines(run_dir)
    kept = [l for l in lines if l.strip().lower() != verb]
    if len(kept) == len(lines):
        return False
    _write(run_dir, kept)
    return True


def answers(run_dir):
    """Every `answer:` line, in file order, as plain text."""
    out = []
    for l in _lines(run_dir):
        s = l.strip()
        if s.lower().startswith("answer:"):
            text = s[len("answer:"):].strip()
            if text:
                out.append(text)
    return out


ANSWER_ID = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*)\s*:\s*(\S.*)$")
# ids of the runner's own shapes may also sit inside one answer that rules on several
ID_INSIDE = re.compile(r"(?<![\w-])([A-Z]{1,3}\d+|[A-Z]-[0-9a-f]{4}|P-\d+)\s*:")


def answers_by_id(run_dir):
    """{id casefolded: text} for every answer that names a stop id, `answer: <id>: <text>`."""
    out = {}
    for text in answers(run_dir):
        m = ANSWER_ID.match(text)
        if m:
            out[m.group(1).casefold()] = m.group(2).strip()
        for sid in ID_INSIDE.findall(text):
            out.setdefault(sid.casefold(), text)
    return out


def note_answers(j, seat):
    """Journal each answer the first time a seat is about to read it; return all of
    them for the brief. The journal, not the file, says which seat reached it."""
    texts = answers(j.dir)
    seen = {e.get("text") for e in j.events() if e.get("kind") == "control" and e.get("verb") == "answer"}
    for t in texts:
        if t not in seen:
            j.append("control", verb="answer", text=t, reached=seat)
    return texts


def answer_status(j):
    """[{text, reached}] for the report: `reached` is the first seat that read it, or
    None when it arrived after every seat."""
    first = {}
    for e in j.events():
        if e.get("kind") == "control" and e.get("verb") == "answer" and e.get("text") not in first:
            first[e["text"]] = e.get("reached")
    return [{"text": t, "reached": first.get(t)} for t in answers(j.dir)]


def cut_requested(run_dir):
    """The launch loop asks this on every poll; a `cut` line is consumed here."""
    return take(run_dir, "cut")


def pause_requested(run_dir):
    return take(run_dir, "pause")


def take_all_next(run_dir):
    """The texts of every queued `next:` line, consumed, in order."""
    lines = _lines(run_dir)

    def taken(line):
        return line.strip().lower().startswith("next:") and bool(line.strip()[len("next:"):].strip())
    texts = [l.strip()[len("next:"):].strip() for l in lines if taken(l)]
    if texts:
        _write(run_dir, [l for l in lines if not taken(l)])
    return texts


def put_next(run_dir, text, first=False):
    """Queue `next: <text>`; `first` puts it before every other line (a resume re-queues)."""
    lines = _lines(run_dir)
    line = f"next: {text}".rstrip()
    _write(run_dir, [line] + lines if first else lines + [line])
