"""hb.run.prune - a run keeps its record and loses its pictures.

    hb page prune [--days 14] [--dry-run]

A run keeps what the page reads (its journal, phase file, control file, pid record, each
seat's report and the two verdict files in evidence) and every path its record commit lists
(`evidence/record.json`), and loses the rest: films,
screenshots, seat streams and briefs. The closeout prunes its run the moment the PR is
merged, keeping the demo; the nightly prune takes any run whose journal has not moved
for the given days and the gate transcripts of that age under evidence; the venue's own
caches are the product's to prune. A run whose process is still there is never touched, and a
link inside a run folder is never followed. The page server runs the nightly once a night.
"""
import json
import os
import re
import stat
import time
from pathlib import Path

from hb.runner import liveness

DAYS = 14
KEEP_TOP = {"journal.jsonl", "phase.json", "control.txt", liveness.FILE}
# the closeout writes the brief and the demo debt right before it prunes; both are the merge's record
KEEP_EVIDENCE = {"closeout.json", "first-screen.json", "demo-owed.json"}


def _kept(rel, keep_dirs, keep_paths=()):
    parts = rel.parts
    if rel.as_posix() in keep_paths:
        return True  # the record commit lists it; a referenced artifact is never pruned
    if len(parts) == 1:
        return parts[0] in KEEP_TOP
    if parts[0] == "seats" and len(parts) == 3:
        return parts[2] == "report.json"
    if parts[0] == "evidence" and len(parts) == 2:
        return parts[1] in KEEP_EVIDENCE
    return parts[0] == "evidence" and len(parts) > 2 and parts[1] in keep_dirs


def _is_link(entry):
    """A symlink or a Windows junction: removing through one deletes what it points at."""
    st = entry.stat(follow_symlinks=False)
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _walk(base, d, rows, dirs, keep_dirs, keep_paths=()):
    """Every file under d that is not kept, and every folder deepest first; a link is
    left where it is and never entered."""
    try:
        entries = list(os.scandir(d))
    except OSError:
        return
    for e in entries:
        if _is_link(e):
            continue
        if e.is_dir(follow_symlinks=False):
            _walk(base, e.path, rows, dirs, keep_dirs, keep_paths)
            dirs.append(Path(e.path))
        elif not _kept(Path(e.path).relative_to(base), keep_dirs, keep_paths):
            rows.append(Path(e.path))


def _delete(rows, dirs, dry_run):
    size = 0
    for f in rows:
        try:
            size += f.stat().st_size
            if not dry_run:
                f.unlink()
        except OSError:
            pass
    for sub in dirs if not dry_run else []:
        try:
            os.rmdir(sub)  # only an empty folder goes
        except OSError:
            pass
    return size


def prune_run(d, dry_run=False, keep_dirs=()):
    """{"id", "files", "bytes"} for one run, or None when it is live or has nothing to lose.
    keep_dirs names evidence subfolders that stay (the closeout keeps "demo")."""
    from hb.record import record
    d = Path(d)
    if liveness.alive(d):
        return None
    rows, dirs = [], []
    _walk(d, d, rows, dirs, set(keep_dirs), record.references(d))
    if not rows:
        return None
    return {"id": d.name, "files": len(rows), "bytes": _delete(rows, dirs, dry_run)}


def prune(root, days=DAYS, dry_run=False, now=None):
    """{"runs": [{"id", "files", "bytes"}], "skipped_live": [...], "bytes": n}."""
    now = now or time.time()
    out = {"runs": [], "skipped_live": [], "bytes": 0, "dry_run": dry_run}
    for e in sorted(os.scandir(root), key=lambda x: x.name) if Path(root).is_dir() else []:
        d = Path(e.path)
        try:
            if _is_link(e) or not e.is_dir() or now - (d / "journal.jsonl").stat().st_mtime < days * 86400:
                continue
        except OSError:
            continue
        if liveness.alive(d):
            out["skipped_live"].append(d.name)
            continue
        row = prune_run(d, dry_run)
        if row:
            out["runs"].append(row)
            out["bytes"] += row["bytes"]
    return out


def prune_gates(evidence_root, days=DAYS, dry_run=False, now=None):
    """Gate transcripts older than days go; the ledger's receipts already hold each
    TRUE_EXIT line. {"files", "bytes"}."""
    now = now or time.time()
    rows = []
    for gates in Path(evidence_root).glob("*/gates") if Path(evidence_root).is_dir() else []:
        for f in gates.glob("*.txt"):
            try:
                if now - f.stat().st_mtime >= days * 86400:
                    rows.append(f)
            except OSError:
                pass
    return {"files": len(rows), "bytes": _delete(rows, [], dry_run), "dry_run": dry_run}


def _transcripts_dir(worktree):
    """Where Claude Code keeps the transcripts of sessions whose working folder was worktree."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(worktree))


def _remove_tree(p):
    """Bytes removed; a folder holding a link is left whole, removing through one deletes its target."""
    p = Path(p)
    if not p.is_dir():
        return 0
    rows, dirs = [], []
    _walk(p, p, rows, dirs, set())
    size = _delete(rows, dirs, False)
    try:
        os.rmdir(p)
    except OSError:
        pass
    return size


def collapse(run_dir, evidence_root):
    """The end of a merged run, once its demo is filmed or none is owed: the run and the slice
    runs a wave composed keep only their record and the demo, and each one's issue evidence and the Claude
    transcripts of its worktree go. The ledger rows and the PR already hold what they proved."""
    from hb.record import view
    run_dir = Path(run_dir)
    from hb.record.view import _events
    evs = _events(run_dir)
    waved = any(e.get("kind") == "slice" for e in evs)
    runs = [run_dir] + ([run_dir.parent / row["run_id"] for row in view._slices(run_dir.name, evs) if row.get("run_id")]
                        if waved else [])
    out = {"runs": [], "bytes": 0}
    for d in dict.fromkeys(runs):
        try:
            phase = json.loads((d / "phase.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            phase = {}
        size = (prune_run(d, keep_dirs=("demo",)) or {}).get("bytes", 0)  # the video is still to be sent
        issue = str(phase.get("issue") or "").lstrip("#")
        if issue:
            size += _remove_tree(Path(evidence_root) / issue)
        if phase.get("worktree"):
            size += _remove_tree(_transcripts_dir(phase["worktree"]))
        out["runs"].append({"id": d.name, "issue": issue or None, "bytes": size})
        out["bytes"] += size
    return out


def nightly(runs_root, evidence_root, days=DAYS, dry_run=False):
    return {"runs": prune(runs_root, days, dry_run), "gates": prune_gates(evidence_root, days, dry_run)}
