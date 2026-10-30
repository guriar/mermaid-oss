"""hb.run.manager_notes - what a run tells the owner it changed, and its changelog lines.

Every build seat writes `for_user` (plain sentences), `underneath` (what changed below,
risks named) and one `changelog` line. A slice joins its seats' lines; a wave joins every
slice's. The same lines go two places from one source: the PR's "What this is" block and
the product's changelog (`paths.changelog_file`), under one `## Unreleased: <title>` section the run commits beside its ledger
rows, so no PR lands without telling the owner what it did.
"""
import json
import re
from pathlib import Path

from hb.rig.common import state_dir
from hb.record.journal import LANDED


def _unique(lines):
    seen, out_ = set(), []
    for line in lines:
        text = re.sub(r"\s+", " ", str(line or "")).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out_.append(text)
    return out_


def join(notes):
    """One note's owner lines from a list of build notes, in the order the seats wrote them."""
    notes = [n for n in notes or [] if isinstance(n, dict)]
    return {"for_user": _unique(x for n in notes for x in n.get("for_user") or []),
            "underneath": _unique(x for n in notes for x in n.get("underneath") or []),
            "changelog_lines": _unique(n.get("changelog") for n in notes)}


def _build_reports(run_id):
    seats = state_dir() / "runs" / str(run_id) / "seats"
    if not run_id or not seats.exists():
        return []
    reports = []
    for path in sorted(seats.glob("build-*/report.json")) + sorted(seats.glob("fix-*/report.json")):
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return reports


def from_slice_runs(results):
    """A wave's owner lines: each landed slice's build notes, read from that slice's own run."""
    merged = {"for_user": [], "underneath": [], "changelog_lines": []}
    for res in results or []:
        if res.get("verdict") not in ("built",) + LANDED:
            continue
        part = join(_build_reports(res.get("run_id")))
        for key in merged:
            merged[key] += part[key]
    return {key: _unique(lines) for key, lines in merged.items()}


def section_title(contract):
    """The changelog section a run writes under: the wave's own line, else the issue title
    without its `slice:` or `hotfix:` prefix."""
    title = str(contract.get("headline") or contract.get("goal") or contract.get("issue") or "").strip()
    title = re.sub(r"^(slice|hotfix|wave anchor|wave)\s*:\s*", "", title, flags=re.I)
    title = re.split(r",\s", title, maxsplit=1)[0].strip()
    return (title[:1].upper() + title[1:]) if title else "Unreleased change"


def write_changelog(wt, contract, lines, changelog_file):
    """Put `lines` under `## Unreleased: <title>` in the product's changelog, newest section
    first, a line already there not written twice. Returns the lines written."""
    path = Path(wt) / changelog_file
    lines = [str(x).strip() for x in lines or []]
    lines = [x for x in lines if x]
    if not lines or not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    fresh = [x for x in lines if f"- {x}" not in text]
    if not fresh:
        return []
    heading = f"## Unreleased: {section_title(contract)}"
    body = text.splitlines()
    if heading in body:
        at = body.index(heading) + 1
        while at < len(body) and body[at].strip() == "":
            at += 1
        while at < len(body) and body[at].startswith("- "):
            at += 1
        body[at:at] = [f"- {x}" for x in fresh]
    else:
        first = next((i for i, line in enumerate(body) if line.startswith("## ")), len(body))
        body[first:first] = [heading, ""] + [f"- {x}" for x in fresh] + [""]
    path.write_text("\n".join(body) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    return fresh
