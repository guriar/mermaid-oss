"""hb.gh - the GitHub reads: the passthrough verb and an issue's GOAL block."""

import re
import sys

from hb.rig.common import _GOAL_TEXT_CACHE, die, gh


def cmd_gh(a):
    """Passthrough: `hb gh -- <gh args>` runs gh under the mutex. The migration
    seam for command files' direct gh calls (P3 migrates them onto this).
    Streams gh's stdout/stderr verbatim and mirrors its exit code, callers keep
    their existing output parsing."""
    args = list(a.ghargs)
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        die("usage: hb gh -- <gh args>")
    rc, o, e = gh(args, timeout=300)
    if o:
        print(o)
    if e:
        print(e, file=sys.stderr)
    sys.exit(rc)


def _wave_goal_text(c, anchor):
    if not anchor:
        return None
    key = str(anchor).lstrip("#")
    if key in _GOAL_TEXT_CACHE:
        return _GOAL_TEXT_CACHE[key]
    rc, body, _ = gh(["issue", "view", key, "--repo", c["repo"],
                      "--json", "body", "-q", ".body"])
    text = None
    if rc == 0 and body:
        m = re.search(r"##\s*GOAL\s*\n(.*?)(?:\n##|\Z)", body, re.S)
        text = (m.group(1).strip() if m else None)
    _GOAL_TEXT_CACHE[key] = text
    return text


def goal_rows_from_text(text):
    """The anchor's `## GOAL` block as rows [(id, when, then)]. A line that names its id
    (`- G2: when ...`, `- **G2** when ...`) keeps it; a line that does not is numbered by
    its position. The wave door and the ledger both read THIS, so the ids a wave drives
    are the ids the ledger books."""
    rows = []
    lines = [l for l in (text or "").splitlines() if l.strip().startswith("-")]
    for k, line in enumerate(lines, 1):
        body = line.strip().lstrip("- ").strip()
        m = re.match(r"\**\s*(G\d+)\**\s*[:.)-]?\s*(.*)$", body)
        gid, rest = (m.group(1), m.group(2)) if m else (f"G{k}", body)
        wm = re.match(r"when\s+(.+?),?\s+then\s+(.+)$", rest, re.I | re.S)
        when, then = (wm.group(1).strip(), wm.group(2).strip()) if wm else (rest.strip(), "")
        rows.append((gid, when, then))
    return rows


def _anchor_goal_ids(c, anchor):
    """The G ids a wave anchor DECLARES in its `## GOAL` block, in order.

    A wave's goals were the only contract items nobody could file a row for: the
    manifest carries the slice items, the anchor carries the goals, and the
    ledger knew only the first list. So a wave's goals arrived as prose in a
    summary, which is a claim, while every item beside them was a measured row.
    The ids come from the SAME block `_wave_goal_text` already reads (one gh
    call per anchor per process, cached above), so the anchor stays the single
    source of what the wave set out to do.

    Returns (ids, why). `why` says what was missing when the list is empty,
    because an unreadable anchor and a goal-less one are different facts."""
    key = str(anchor or "").lstrip("#")
    if not key:
        return [], "no anchor was named"
    text = _wave_goal_text(c, key)
    if text is None:
        return [], f"issue #{key} could not be read, or carries no `## GOAL` block"
    ids = []
    for gid, _w, _t in goal_rows_from_text(text):
        if gid not in ids:
            ids.append(gid)
    return ids, (None if ids else f"the `## GOAL` block on #{key} carries no goal lines")
