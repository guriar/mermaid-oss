"""hb.rig - the harness itself: `rig drill` drills a commit once on a frozen checkout and keeps
its stamp; `rig structure` checks the seven foundations and the arrow; `rig words` refuses a
product's word in the engine. The plugin is the
install; a run pins itself at launch (hb.versions).
"""

import os
import re

from hb.rig import structure, versions, words
import subprocess
import sys
from pathlib import Path

from hb.rig.common import canon, cfg, out

# ── the install ───────────────────────────────────────────────────────────


def _source_dir(a=None):
    """Where Mermaid is on this host. Explicit flag, env, config, then the tree this
    engine is running out of, then a sibling of canon."""
    for cand in (getattr(a, "source", None) if a else None,
                 os.environ.get("HB_MERMAID_DIR"),
                 ((cfg().get("rig") or {}).get("source_dir"))):
        if cand:
            return Path(cand)
    here = Path(__file__).resolve().parents[3]
    # a laid-out version is not the source: it has no history to read a branch tip from
    if (here / "engine" / "hb.py").exists() and (here / "commands").is_dir() and (here / ".git").exists():
        return here
    return canon().parent / "Mermaid"


def _source_branch():
    """The branch whose tip `rig drill` drills; a product fact, so it has no default."""
    branch = (cfg().get("rig") or {}).get("source_branch")
    if not branch:
        out({"verdict": "refused", "why": "rig.source_branch is not set in harness-config.json; rig drill has no default branch"}, 2)
    return branch


def discover_drills(src: Path, only=None):
    """Every drill under drills/<foundation>/, found by glob, so a new drill costs a file and
    never a list edit; `only` names one foundation folder or one drill as <foundation>/<file>.py. drills/fixture/ holds helpers, not drills."""
    root = Path(src) / "drills"
    files = sorted(p for p in root.glob("*/*.py") if p.parent.name != "fixture" and p.name != "__init__.py"
                   and (not only or p.parent.name == only or p.relative_to(root).as_posix() == only))
    return [p.relative_to(src).as_posix() for p in files]


def _alone(src: Path, rel):
    """A drill that declares `ALONE = True` times the host's own waits; it runs after the
    parallel set so a loaded host never reads as a red."""
    return "\nALONE = True" in (Path(src) / rel).read_text(encoding="utf-8", errors="replace")


def read_true_exit(text):
    """(state, line) from a drill's output. Green is the bare line `TRUE_EXIT=0` and nothing
    else: `TRUE_EXIT=0 UNMEASURED` measured nothing, `TRUE_EXIT=0 SKIPPED <why>` chose not to
    on this host, and a missing line is red, because a drill that never printed its verdict
    proved nothing (the install once read an unmeasured drill as green)."""
    lines = re.findall(r"^TRUE_EXIT=.*$", text or "", re.M)
    if not lines:
        return "red", "TRUE_EXIT=missing"
    last = lines[-1].strip()
    if last == "TRUE_EXIT=0":
        return "green", last
    if last.startswith("TRUE_EXIT=0 UNMEASURED"):
        return "unmeasured", last
    if re.match(r"TRUE_EXIT=0 SKIPPED \S", last):
        return "skipped", last
    if last.startswith("TRUE_EXIT=0 SKIPPED"):
        return "red", f"{last} (skipped without a reason)"
    return "red", last


def run_drills(src: Path, only=None):
    """(all_green, rows). Each row quotes the drill's own TRUE_EXIT line, never a summary; a
    run is green when every drill is green or skipped with a reason.

    HB_RIG_STUB_DRILLS=green|red stands in for the run so a drill of this verb can
    exercise both answers without a nested drill suite.
    """
    drills = discover_drills(src, only)
    stub = os.environ.get("HB_RIG_STUB_DRILLS")
    if stub in ("green", "red"):
        line = "TRUE_EXIT=0" if stub == "green" else "TRUE_EXIT=1"
        return stub == "green", [{"drill": d, "TRUE_EXIT": line, "state": stub, "stubbed": stub} for d in drills]

    def one(rel):
        p = Path(src) / rel
        # a timeout catches a hung drill, not a slow one: the wave drill ran 39 minutes green on a loaded host
        try:
            r = subprocess.run([sys.executable, str(p)], cwd=str(src), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=3600,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            rc, text = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            rc, text = 124, "timeout after 3600s"
        keep = os.environ.get("HB_DRILL_OUT")
        if keep:
            # the whole output, kept where CI can upload it: a row's tail is 400 characters and a red needs the rest
            dest = Path(keep) / (rel.replace("/", "-") + ".txt")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        state, line = read_true_exit(text)
        if rc and state != "red":
            state, line = "red", f"{line} (process exit {rc})"
        # each row is printed the moment its drill ends, so a host that dies mid-run still leaves the rows before it
        print(f"{line:34s} {rel}", file=sys.stderr, flush=True)
        return {"drill": rel, "TRUE_EXIT": line, "state": state, "rc": rc, "tail": text[-400:]}

    # each drill builds its own temp repos and stub venues, so they run side by side, as many as
    # the host has cores (HB_DRILL_WORKERS overrides): all at once starved a hosted CI runner to death
    from concurrent.futures import ThreadPoolExecutor
    together = [d for d in drills if not _alone(src, d)]
    workers = int(os.environ.get("HB_DRILL_WORKERS") or os.cpu_count() or 2)
    rows = []
    if together:
        with ThreadPoolExecutor(max_workers=min(workers, len(together))) as pool:
            rows = list(pool.map(one, together))
    rows += [one(d) for d in drills if _alone(src, d)]
    return all(r["state"] in ("green", "skipped") for r in rows), rows


def drilled(src: Path, sha):
    """(green, rows, fresh): the commit's drill stamp, running the drills on the frozen
    version when it has none. A commit is drilled once; a red stamp is drilled again."""
    stamp = versions.read_stamp(sha)
    if stamp and stamp.get("green") and not os.environ.get("HB_RIG_STUB_DRILLS"):
        return True, stamp.get("drills") or [], False
    if os.environ.get("HB_RIG_STUB_DRILLS"):
        green, rows = run_drills(src)
        return green, rows, True
    tree, why = versions.drill_checkout(src, sha)
    if not tree:
        return False, [{"drill": "checkout", "TRUE_EXIT": None, "why": why}], True
    try:
        green, rows = run_drills(tree)
    finally:
        versions.remove_tree(tree)
    versions.write_stamp(sha, green, rows)
    return green, rows, True


def cmd_rig_drill(a):
    """Drill the branch tip's COMMIT on a frozen copy and keep the result as its stamp. The
    working copy is free to change while this runs."""
    if getattr(a, "here", False):
        src = Path(getattr(a, "source", None) or versions.tree_root())
        green, rows = run_drills(src, getattr(a, "only", None))
        if not rows:
            out({"verdict": "no-drills", "why": f"nothing under drills/ matches {getattr(a, 'only', None)!r}; nothing was measured"}, 1)
        counts = {k: sum(1 for r in rows if r["state"] == k) for k in ("green", "skipped", "unmeasured", "red")}
        out({"verdict": "green" if green else "red", "counts": counts, "drills": rows}, 0 if green else 1)
    src = _source_dir(a)
    branch = _source_branch()
    sha = versions.full_sha(src, branch)
    if not sha:
        out({"verdict": "no-source", "why": f"no branch {branch} to drill in {src}"}, 2)
    green, rows, fresh = drilled(src, sha)
    out({"verdict": "green" if green else "red", "commit": sha[:versions.SHA_LEN], "fresh": fresh,
         "stamp": str(versions.stamp_path(sha)), "drills": rows}, 0 if green else 1)


def cmd_rig(a):
    {"drill": cmd_rig_drill, "structure": structure.cmd_rig_structure, "words": words.cmd_rig_words}[a.op](a)


def register(sub):
    s = sub.add_parser("rig", help="the harness itself: drill a commit, check its structure")
    s.add_argument("op", choices=["drill", "structure", "words"])
    s.add_argument("--include-product", dest="include_product", action="store_true", help="words: scan product/ too")
    s.add_argument("--root", help="structure: the hb package to check (default: this engine)")
    s.add_argument("--here", action="store_true", help="drill: run the drills on this working tree, no frozen checkout and no stamp")
    s.add_argument("--only", help="drill: one foundation folder under drills/, or one drill as <foundation>/<file>.py")
    s.add_argument("--from", dest="source",
                   help="drill: the mermaid checkout (else HB_MERMAID_DIR, else "
                        "rig.source_dir in harness-config.json, else the tree hb runs from)")
