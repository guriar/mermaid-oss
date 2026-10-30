"""hb.run.usage - what a run's seats spent, by local day, and the last plan limit they hit.

The header's meters add these up across runs: Claude agent minutes and list-price dollars,
Codex tokens. Only a seat that ended counts; a working seat's spend is unknown until then.
A limit is read from the seat's own stream, where the CLI writes `api_error`
`usage_limit_reached` with the time the plan is back. A builder's helpers spend under the
builder (A17): their calls, tokens and minutes ride its seat line and the run's total, and
the skills a seat invoked ride beside them (Q6).
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

from hb.runner import adapters

TAIL_BYTES = 64 * 1024
_SEEN = {}


def _day(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return None


def limit_hit(stream):
    """{"resets": epoch or None, "kind": ...} when the stream's tail carries a usage limit,
    else None. Kept while the file is unchanged."""
    try:
        st = os.stat(stream)
    except OSError:
        return None
    key = str(stream)
    hit = _SEEN.get(key)
    if hit and hit[0] == (st.st_size, st.st_mtime_ns):
        return hit[1]
    try:
        with open(stream, "rb") as f:
            f.seek(max(0, st.st_size - TAIL_BYTES))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    # one reader of the stream's limit fields: the seat adapter's, which also parks the run
    hit_ = adapters.usage_limit(text)
    found = {"resets": hit_.get("resets_epoch"), "kind": hit_.get("kind")} if hit_ else None
    _SEEN[key] = ((st.st_size, st.st_mtime_ns), found)
    return found


HELPER_TOOLS = ("Agent", "Task")
SKILL_TOOL = "Skill"


def helpers(stream):
    """{"calls", "tokens", "minutes", "skills_invoked"} the seat's helpers spent and the skills
    the seat itself invoked, read off its stream-json, or None when it did neither. Each helper's
    result carries its own totals; a stream without them sums the helper's turns, which carry a
    `parent_tool_use_id`, and leaves minutes unknown."""
    calls, tokens, ms, turn_tokens = set(), 0, 0, 0
    skills = []
    try:
        f = open(stream, encoding="utf-8", errors="replace")
    except (OSError, TypeError):
        return None
    with f:
        for line in f:
            if '"tool_use' not in line and '"parent_tool_use_id"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            # a user turn's message can be a bare string, not a dict
            message = o.get("message") if isinstance(o.get("message"), dict) else {}
            content = message.get("content") or []
            if o.get("type") == "assistant" and o.get("parent_tool_use_id"):
                u = message.get("usage") or {}
                turn_tokens += adapters.context_of(u) + int(u.get("output_tokens") or 0)
                continue
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") in HELPER_TOOLS:
                    calls.add(block.get("id"))
                elif block.get("name") == SKILL_TOOL:
                    name = str((block.get("input") or {}).get("skill") or "").strip()
                    if name and name not in skills:
                        skills.append(name)
            done = o.get("tool_use_result")
            results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] if isinstance(content, list) else []
            ids = {b.get("tool_use_id") for b in results}
            if isinstance(done, dict) and ids & calls:
                tokens += int(done.get("totalTokens") or 0)
                ms += int(done.get("totalDurationMs") or 0)
    if not calls and not skills:
        return None
    return {"calls": len(calls), "tokens": tokens or turn_tokens or None,
            "minutes": round(ms / 60000, 1) if ms else None, "skills_invoked": skills}


# the seat roles on the new shape (Q9): a fix round is the builder's, the second reader is the
# review's, an old run's drive is the audit's; there is no fix or verify line
SEAT_ROLES = ("build", "review", "discovery", "audit", "merge")
ROLE_OF = {"fix": "build", "codex": "review", "verify": "audit"}
# the name the page shows for a seat's role
ROLE_NAMES = {"build": "Builder", "review": "Reviewer", "verify": "Tester", "fix": "Builder, fixing", "merge": "Merger",
              "codex": "Second reader", "before": "Before capture", "discovery": "Discovery tester", "audit": "Auditor"}


def label_role(label):
    """The role a seat label names: `verify.2` and `merge-8` are a verify and a merge seat."""
    return str(label or "").split(".")[0].split("-")[0]


def seat_role(label):
    """The role a seat's cost counts under."""
    return ROLE_OF.get(label_role(label), label_role(label))


def role_name(label, default="A seat"):
    """The page's name for a seat: its role's name, and the shard a `.n` label carries."""
    role = label_role(label)
    name = ROLE_NAMES.get(role, role.capitalize() or default)
    return name + (f" {str(label).split('.', 1)[1]}" if "." in str(label or "") else "")


def ended_seats(evs):
    """The seat lines that count toward spend: a seat that ended, never a merged duplicate."""
    return [e for e in evs if e.get("kind") == "seat" and e.get("state") not in ("started", None) and not e.get("merged")]


def spent(evs):
    """The one sum of what a run's seats spent, read by the PR, the page and the header:
    {"minutes", "usd", "steps", "by_role" (minutes), "peak_context_tokens", "helpers" (calls,
    tokens, minutes), "skills" (by role), "days" {date: [claude_min, claude_usd, codex_tokens]}}."""
    minutes = usd = 0.0
    by_role, skills, days, peak, steps = {}, {}, {}, 0, 0
    helped = {"calls": 0, "tokens": 0, "minutes": 0.0}
    for e in ended_seats(evs):
        u = e.get("usage") if isinstance(e.get("usage"), dict) else {}
        role = seat_role(e.get("seat"))
        steps += 1
        peak = max(peak, int(u.get("peak_context_tokens") or 0))
        for k in helped:
            helped[k] += (u.get("helpers") or {}).get(k) or 0
        for name in (u.get("helpers") or {}).get("skills_invoked") or []:
            if name not in skills.setdefault(role, []):
                skills[role].append(name)
        day = _day(e.get("ts"))
        row = days.setdefault(day, [0.0, 0.0, 0]) if day else [0.0, 0.0, 0]
        if e.get("runtime") == "codex":
            row[2] += int(u.get("total_tokens") or 0)
            continue
        m, cost = float(e.get("minutes") or 0), float(u.get("cost_usd") or 0)
        minutes, usd = minutes + m, usd + cost
        by_role[role] = by_role.get(role, 0) + m
        row[0], row[1] = row[0] + m, row[1] + cost
    return {"minutes": round(minutes, 1), "usd": round(usd, 2), "steps": steps,
            "by_role": {k: round(v) for k, v in by_role.items()}, "peak_context_tokens": peak or None,
            "helpers": dict(helped, minutes=round(helped["minutes"], 1)), "skills": skills,
            "days": {k: [round(v[0], 1), round(v[1], 2), v[2]] for k, v in days.items()}}


def of(run_dir, evs):
    """The header's reading of one run: its spend by local day (spent), the last plan limit a
    seat hit, and the helpers' totals."""
    s, limit = spent(evs), None
    for e in ended_seats(evs):
        if e.get("state") != "ok":
            hit = limit_hit(Path(run_dir) / "seats" / f"{e.get('seat')}-{e.get('attempt')}" / "stdout.txt")
            if hit:
                limit = dict(hit, at=e.get("ts"))
    return {"days": s["days"], "limit": limit, "helpers": s["helpers"]}


def meters(rows, now=None):
    """Today's and the last seven days' spend across every run, and the last limit hit."""
    now = now or time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    week = {time.strftime("%Y-%m-%d", time.localtime(now - i * 86400)) for i in range(7)}
    sums = {"today": [0.0, 0.0, 0], "week": [0.0, 0.0, 0]}
    limit = None
    for r in rows:
        u = r.get("usage") or {}
        for day, vals in (u.get("days") or {}).items():
            for span, keep in (("today", day == today), ("week", day in week)):
                if keep:
                    sums[span] = [a + b for a, b in zip(sums[span], vals)]
        hit = u.get("limit")
        if hit and (not limit or str(hit.get("at")) > str(limit.get("at"))):
            limit = hit
    out = {span: {"agent_min": round(v[0]), "list_usd": round(v[1], 2), "codex_tokens": v[2]} for span, v in sums.items()}
    # a limit that has already reset says nothing about now
    out["limit"] = limit if limit and (limit.get("resets") or 0) > now else None
    return out
