"""hb.run.success - the `## Success` block a sitting writes on an anchor or a slice.

GOAL says what is proven at merge; Success says what the topic should move after it: the
number, where it is read (the product's analytics, its traces, the database or the battery), today's value,
the target and when it is looked at. The block is parsed like GOAL, carried into the record
and the PR synopsis, and the closeout writes a `success-check` event due when the block
says, counted from the merge time the PR reports, that `hb run watch` surfaces; with no
readable merge time nothing is written, since a due date counted from now would be wrong for
good. An entry whose `when` names no span is refused at parse time (`problems`), so every
entry has a due date. The check itself is a later command; the record holds what it needs.

    ## Success
    - number: pull cards OK with no edit
      read: analytics
      today: 6 of 10
      target: 9 of 10
      when: two weeks after the merge
"""
import re
from datetime import datetime, timedelta

KEYS = ("number", "read", "today", "target", "when")
_WORDS = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_DAYS = {"day": 1, "week": 7, "fortnight": 14, "month": 30}
_SPAN = re.compile(r"\b(\d+|" + "|".join(_WORDS) + r")\s+(" + "|".join(_DAYS) + r")s?\b", re.I)


def parse(section):
    """The block's entries, one per `- ` bullet, each a dict of KEYS and `after_days` from
    its `when`. An entry missing a key carries it as None; a `when` with no span reads None."""
    entries, cur = [], None
    for line in str(section or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("- "):
            cur = {k: None for k in KEYS}
            entries.append(cur)
            s = s[2:].strip()
        if cur is None:
            continue
        m = re.match(r"^(\w+)\s*:\s*(.+?)\s*$", s)
        if m and m.group(1).lower() in KEYS:
            cur[m.group(1).lower()] = m.group(2)
    for e in entries:
        e["after_days"] = after_days(e.get("when"))
    return entries


def after_days(text):
    """Days after the merge the block's `when` names, or None when it names no span."""
    m = _SPAN.search(str(text or ""))
    if not m:
        return None
    n = int(m.group(1)) if m.group(1).isdigit() else _WORDS[m.group(1).lower()]
    return n * _DAYS[m.group(2).lower()]


def problems(entries):
    """One line per entry the closeout could never date: a `when` that names no span."""
    return [f"Success entry `{e.get('number') or 'unnamed'}`: `when` names no span the check can count "
            f"(\"{e.get('when') or ''}\"); say it in days, weeks, fortnights or months" for e in entries or []
            if e.get("after_days") is None]


def merged_time(merged_at):
    """The merge time the PR reports, or None when it reports none that reads."""
    try:
        return datetime.fromisoformat(str(merged_at).replace("Z", "+00:00")) if merged_at else None
    except ValueError:
        return None


def due(entries, merged_at):
    """The check's due date for each entry, from the merge time the PR reports."""
    base = merged_time(merged_at)
    out = []
    for e in entries or []:
        days = e.get("after_days")
        out.append(dict(e, due=(base + timedelta(days=days)).date().isoformat() if days else None))
    return out


def lines(entries):
    """One line per entry, for the record and the PR."""
    return [f"{e.get('number') or 'unnamed number'}, read in {e.get('read') or 'an unnamed source'}: today {e.get('today') or '?'}, "
            f"target {e.get('target') or '?'}, looked at {e.get('when') or 'no time named'}"
            + (f" ({e['after_days']} days after the merge)" if e.get("after_days") else "")
            + (f", due {e['due']}" if e.get("due") else "")
            for e in entries or []]
