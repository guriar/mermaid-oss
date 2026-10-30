"""hb.common - plumbing every seam stands on: exit, exec, output, config, paths, clocks."""

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

# The file every caller invokes. A seam module's own __file__ points inside the
# package, so a re-invocation names this, never the module it happens to sit in.
HB_ENTRY = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "hb.py"))


def die(msg, code=3, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}))
    sys.exit(code)


def out(obj, code=0):
    obj.setdefault("ok", code == 0)
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(code)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd, timeout=60, **kw):
    """Run a command list; return (rc, stdout, stderr). Never raises on rc!=0."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", **kw)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", str(e)


def canon():
    rc, o, _ = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if rc != 0:
        die("not in a git repo")
    return Path(o).parent

def branch_setting(c, key):
    """One `branches` key from the config (docs/config.md); a key the config lacks is a refusal naming it."""
    value = (c.get("branches") or {}).get(key)
    if not value:
        die(f"harness-config.json branches.{key} is not set", 2)
    return value


def _P(key):
    """A product path from harness-config.json `paths`. No path has a default (docs/config.md):
    a key the config lacks is a refusal naming it. Read lazily so `hb --help` works outside a repo."""
    value = (cfg_or_empty().get("paths") or {}).get(key)
    if value is None:
        die(f"harness-config.json paths.{key} is not set", 2)
    return value


def invoking_root():
    """The git worktree the CALLER stands in, or None when cwd is not inside one.

    `canon()` resolves
    `--git-common-dir`, which from ANY linked worktree points back at the canon
    checkout, so every path hb built resolved against canon whatever tree the
    caller stood in, following neither cwd nor any flag. ship-slice v8 mandates
    worktrees, so that was every slice.

    ABSENT is a named state here. None means "cwd is not inside a worktree", never
    "use canon": the caller picks the fallback and says which it used.
    """
    rc, o, _ = run(["git", "rev-parse", "--show-toplevel"])
    if rc != 0 or not o.strip():
        return None
    return Path(o.strip())


def doc_root():
    """Where the REPO'S OWN documents live for this invocation: the contract
    manifests, the delivery ledger, the class-(b) registry. The INVOKING worktree,
    falling back to canon only when cwd is not inside a git worktree.

    Contrast `canon()`, which stays correct for harness STATE (
    evidence, locks): that state is shared across every worktree by design, and
    moving it per-tree would split a slice's own record in half.
    """
    return invoking_root() or canon()


def cfg():
    p = canon() / ".claude" / "harness-config.json"
    if not p.exists():
        die(f"missing machine config {p}, project layer not provisioned")
    return json.loads(p.read_text(encoding="utf-8"))


def cfg_or_empty():
    """The machine config, or {} where none is provisioned (a drill, a bare host): for a
    reader whose every key has a default, so a missing file is no refusal."""
    p = canon() / ".claude" / "harness-config.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def state_dir(c=None):
    c = c or cfg()
    d = canon() / c["state_dir"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def atomic_write(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def os_lock_acquire(lock_path, timeout=120):
    """A cross-process mutex on one file, held through an OS lock (msvcrt or fcntl) that
    the OS drops when its process dies, so a crash never leaves it held. Returns the
    open handle for os_lock_release; raises TimeoutError past `timeout` seconds."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+b")
    deadline = time.time() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.time() > deadline:
                f.close()
                raise TimeoutError(f"{lock_path.name} busy for >{timeout}s")
            time.sleep(0.05)


def os_lock_release(f):
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        f.close()


def _gh_lock_acquire(timeout=120):
    """gh 401s under concurrent invocation on this host (Windows credential-manager
    contention), so every gh call on the host queues on one file under the canon."""
    return os_lock_acquire(canon() / ".claude" / "harness" / "gh.lock", timeout)


_gh_lock_release = os_lock_release


def gh(args, timeout=45):
    env = dict(os.environ)
    env["MSYS_NO_PATHCONV"] = "1"
    # HB_GH_STUB (drills-only seam, F8 2026-07-29): path to a python script that
    # answers gh's argv from fixtures, lets books' refusal ladder drill OFFLINE.
    # A .bat/.sh shim can't intercept here: subprocess uses CreateProcess, which
    # resolves bare names to .exe only. Absent in production, this is a no-op.
    stub = env.get("HB_GH_STUB")
    base = [sys.executable, stub] if stub else ["gh"]
    try:
        lk = _gh_lock_acquire()
    except TimeoutError as e:
        return 125, "", str(e)
    try:
        return run(base + args, timeout=timeout, env=env)
    finally:
        _gh_lock_release(lk)


# ── ci-watch / deploy-watch ───────────────────────────────────────────────


def _probe_outage(c):
    """Distinguish 'their CI failed' from 'the provider is down'."""
    try:
        req = urllib.request.Request(c["ci"]["outage_probe"], headers={"User-Agent": "hb"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return {"api_reachable": True, "http": r.status}
    except Exception as e:
        try:
            with urllib.request.urlopen(c["ci"]["status_page"], timeout=10) as r:
                s = json.loads(r.read().decode())
                return {"api_reachable": False, "status_page": s.get("status", {})}
        except Exception:
            return {"api_reachable": False, "status_page_error": str(e)[:120]}


# What a test path is: one rule for the record's retired tests and the size tripwire.
_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|e2e|evals?)(/|$)|(^|/)test_[^/]*\.py$|"
    r"[^/]*\.(test|spec)\.[jt]sx?$|(^|/)conftest\.py$", re.I)


def is_test_path(p):
    return bool(_TEST_PATH_RE.search(str(p).replace("\\", "/")))


def _canon_python():
    """(interpreter, why) - the one answer to which python runs the product's code: a gate,
    the test database recipe.

    The product config's `dep_parity.venv_python` (relative to canon), else the interpreter
    running the engine. Named rather than silent, because "which python ran this" is the
    difference between a gate that measured the project and one that measured an empty one.
    """
    configured = (cfg_or_empty().get("dep_parity") or {}).get("venv_python")
    if configured:
        p = canon() / configured
        if p.exists():
            return str(p), f"configured venv {Path(configured).as_posix()}"
    return (sys.executable,
            "the engine's own python - no dep_parity.venv_python on disk; project pins "
            "are NOT guaranteed present")


#
# The reviewer node emits review-findings/v1 (quote-gated findings, no score).
# This command derives the 0-5 deterministically, so two runs over the same
# report can never disagree and an over-confident reviewer cannot buy a merge.


def _locked_rmw(path: Path, default: dict, fn):
    """Read-modify-write under ONE lock hold. The docket's load-outside-lock pattern
    is fine for an owner walk (one writer); a registry two LIVE waves hit
    concurrently loses updates without it, acquire A, acquire B, write B, write A
    and A's hold silently vanishes (S1 atomicity drill). fn(m) mutates m in place
    and returns (obj, exit_code); m is always written back (a refusal path that
    didn't mutate writes identical bytes, harmless)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    for _ in range(50):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            time.sleep(0.2)
    else:
        die(f"{path.name} lock held >10s, another writer is stuck; remove the .lock if orphaned", 2)
    try:
        m = json.loads(path.read_text(encoding="utf-8")) if path.exists() else json.loads(json.dumps(default))
        obj, code = fn(m)
        atomic_write(path, json.dumps(m, ensure_ascii=False, indent=1))
        return obj, code
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def _manifest_path(slug, root=None):
    # `doc_root()`, not `canon()`. A manifest amended on the slice branch
    # is invisible from canon, and canon rests on dev.
    return (root or doc_root()) / _P('manifest_dir') / f"{slug}.yaml"


def _ledger_path(root=None):
    # The ledger of the INVOKING worktree. Reading canon's copy
    # reported converted findings as "no ledger row" while the rows existed on the
    # branch; WRITING canon's copy forced a hand-copy that destroyed 10 rows.
    return (root or doc_root()) / _P('ledger_file')


def _sha256_file(p: Path):
    """sha256 over RAW BYTES. Deliberately not line-ending normalized: a mock
    rewritten by an editor that flips CRLF IS a different artifact, and §0 rules
    that a mutable mock is not an identity. Returns None when unreadable, the
    caller must render that as UNMEASURED, never as a mismatch."""
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ── a strict YAML SUBSET reader (no new deps) ────────────────────────────────
#
# PyYAML is not installed on this host (measured: ModuleNotFoundError, python
# 3.11.9) and hb.py is single-file/no-deps by contract. So the manifest reader
# supports a DECLARED SUBSET and REFUSES everything else by name. That refusal
# is rc=2 `manifest-unparseable`, "this reader cannot measure the file", which
# is NOT the same claim as "the manifest is invalid". A lenient parser
# that guessed at an unsupported construct would produce a confidently wrong
# item list, and every gate downstream keys off that list.
#
# SUBSET: block mappings, block sequences (scalars and mappings), `#` comments,
# single/double quoted scalars, int/float/bool/null, plain strings. REFUSED:
# anchors/aliases, tags, flow (inline) collections, block scalars (| >),
# multi-document streams, tab indentation.


class _YamlSubsetError(Exception):
    """Raised when the manifest uses YAML this reader does not implement.
    Carries the line number: an unparseable file must name WHERE it lost."""


NEWLINE = chr(10)


def _slice_key(v):
    """Slice assignment is compared as a STRING with any leading '#' stripped -
    a manifest may write `slice: 42` (int) or `slice: "#42"`; both name the
    same slice and a type mismatch must never read as 'unassigned'."""
    if v is None:
        return None
    s = str(v).strip().lstrip("#")
    return s or None


def _item_precedence(doc, item):
    """§1.1 precedence is a MANIFEST FIELD, per surface, declared at the sitting -
    it never degrades into a builder's impression. Resolution order: the item's
    own field · the surface's declaration · the manifest default. A value that
    resolves nowhere returns None and the caller must render it UNDECLARED, never
    default it to artifact-first (defaulting an authority field is the honesty-
    parameter defect: CLAUDE.md, an honesty default must CLAIM LESS)."""
    prec = doc.get("precedence") if isinstance(doc.get("precedence"), dict) else {}
    if item.get("precedence"):
        return str(item["precedence"]), "item"
    surf = item.get("surface")
    per = prec.get("surfaces") if isinstance(prec.get("surfaces"), dict) else {}
    if surf and per.get(surf):
        return str(per[surf]), f"surface:{surf}"
    if prec.get("default"):
        return str(prec["default"]), "manifest-default"
    return None, "UNDECLARED"


def _manifest_artifacts(doc, items=None):
    """Every artifact reference reachable from `items` (or the whole manifest),
    as {path: declared_sha256_or_None}. Manifest-level `artifacts:` entries are
    the registry; an item may name one by path."""
    declared = {}
    for a_ in (doc.get("artifacts") or []):
        if isinstance(a_, dict) and a_.get("path"):
            declared[str(a_["path"])] = a_.get("sha256")
    if items is None:
        return declared
    used = {}
    for it in items:
        for key in ("artifact", "mock", "demo"):
            v = it.get(key)
            if not v:
                continue
            for path in ([v] if isinstance(v, str) else v):
                used[str(path)] = declared.get(str(path))
    # An item-level artifact absent from the registry has NO declared hash, the
    # caller refuses on that (an unhashed mock is a mutable mock, §0).
    return used or declared


def _verify_artifacts(doc, items, root=None):
    """Hash every artifact the subset depends on. Returns (rows, problems).
    Three outcomes per artifact, never two: matched · MISMATCH (measured, the
    mock moved under the contract) · UNMEASURED (missing file, unreadable file,
    or no declared hash). They route differently and must never collapse.

    Resolves against the INVOKING worktree (doc_root), not canon, the
    gate was hashing a file no builder in the wave reads (canon rests on dev;
    the slice builds in its worktree). When the two trees hold different bytes
    the row says so, naming both hashes and which tree each came from: a gate
    that cannot say which file it hashed cannot certify identity.

    Declared hashes are computed from COMMITTED BLOBS (LF); this host
    checks out CRLF, so a raw-bytes compare false-positives on EVERY unchanged
    text artifact, which trains readers to discount drift output. Newlines are
    normalized ON COMPARE; a match via
    normalization is named `match_basis: newline-normalized` so it is visible.
    A real one-character content change still mismatches, normalization
    collapses \\r\\n to \\n and nothing else."""
    root = root or doc_root()
    can = canon()
    rows, problems = [], []
    for rel, declared in sorted(_manifest_artifacts(doc, items).items()):
        p = (root / rel) if not Path(rel).is_absolute() else Path(rel)
        row = {"path": rel, "abs": str(p), "hashed_tree": str(root),
               "declared_sha256": declared}
        if not Path(rel).is_absolute() and can != root:
            cp_ = can / rel
            ch = _sha256_file(cp_) if cp_.exists() else None
            wh = _sha256_file(p) if p.exists() else None
            if ch != wh:
                row["tree_divergence"] = {
                    "worktree": {"tree": str(root), "sha256": wh},
                    "canon": {"tree": str(can), "sha256": ch},
                    "note": "the two trees hold DIFFERENT bytes for this "
                            "artifact; the verdict below is about the "
                            "worktree copy (the one the slice builds against)"}
        if not p.exists():
            row["verdict"] = "artifact-missing"
            row["actual_sha256"] = None
            problems.append(f"{rel}: declared by the manifest but NOT ON DISK at "
                            f"{p}, a cited-but-absent artifact is UNMEASURED, "
                            "never a pass")
        elif not declared:
            row["verdict"] = "artifact-unhashed"
            row["actual_sha256"] = _sha256_file(p)
            problems.append(f"{rel}: the manifest declares NO sha256 for this "
                            "artifact, so nothing pins which bytes the contract "
                            "means. A mutable mock is not an identity (§0). "
                            f"Observed content hash: {row['actual_sha256']}")
        else:
            actual = _sha256_file(p)
            row["actual_sha256"] = actual
            norm = None
            if actual is not None and actual != declared:
                try:
                    norm = hashlib.sha256(
                        p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                except OSError:
                    norm = None
            if actual is None:
                row["verdict"] = "artifact-unreadable"
                problems.append(f"{rel}: exists but could not be READ, unreadable "
                                "is UNMEASURED, never a match")
            elif actual == declared:
                row["verdict"] = "artifact-matched"
                row["match_basis"] = "raw"
            elif norm == declared:
                # A CRLF checkout of the LF blob, identical content.
                row["verdict"] = "artifact-matched"
                row["match_basis"] = "newline-normalized"
            else:
                row["verdict"] = "artifact-hash-mismatch"
                row["normalized_sha256"] = norm
                problems.append(f"{rel}: content hash {actual[:16]} (normalized "
                                f"{str(norm)[:16]}) does NOT match the manifest's "
                                f"declared {str(declared)[:16]}, the artifact "
                                "changed under the contract, and it is not a "
                                "line-ending artifact (checked). Re-approve "
                                "it and amend the manifest (a commit citing the "
                                "ruling), or restore the approved bytes.")
        rows.append(row)
    return rows, problems


# ── the delivery ledger (§0 · §5.3) ──────────────────────────────────────────


def _ledger_read(root=None):
    """(events, err). A malformed line is NEVER skipped silently, it is returned
    as a named problem, because a ledger that quietly drops rows is exactly the
    fake-green the ledger exists to prevent."""
    p = _ledger_path(root)
    if not p.exists():
        # dk-family: a never-created ledger must not render as an empty one -
        # absent is a named state, not zero rows.
        return [], {"error": f"delivery ledger at {p} does not exist, "
                             "ABSENT is not the same as EMPTY (no wave has "
                             "ever folded rows, or the path is wrong)",
                    "verdict": "ledger-absent"}
    events, bad = [], []
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as ex:
        return [], {"error": f"delivery ledger at {p} is UNREADABLE ({ex}), "
                             "unreadable is UNMEASURED, never an empty ledger",
                    "verdict": "ledger-unreadable"}
    for n, ln in enumerate(raw.splitlines(), 1):
        if not ln.strip():
            continue
        try:
            events.append(json.loads(ln))
        except json.JSONDecodeError as ex:
            bad.append(f"line {n}: {ex}")
    if bad:
        return events, {"error": f"delivery ledger at {p} has {len(bad)} "
                                 "UNPARSEABLE line(s); they are reported, never "
                                 "skipped, a dropped row reads as 'this item was "
                                 "never measured', which is a different claim from "
                                 "'the file is damaged'",
                        "verdict": "ledger-damaged", "bad_lines": bad[:10],
                        "parsed": len(events)}
    return events, None


def _docket_path():
    return state_dir() / "docket.json"


def _docket_load():
    p = _docket_path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"schema": "docket/v1", "entries": []}


def _pr_state_for_head(c, branch):
    """(state, number) for the PR whose head is `branch`, or (None, None) when
    there is none or gh could not answer. OPEN wins over any other state."""
    if not branch:
        return None, None
    rc, o, _e = gh(["pr", "list", "--repo", c["repo"], "--state", "all", "--head",
                    branch, "--json", "number,state", "--limit", "20"], timeout=60)
    if rc != 0:
        return None, None
    try:
        rows = json.loads(o or "[]")
    except ValueError:
        return None, None
    for r in rows:
        if r.get("state") == "OPEN":
            return "OPEN", r.get("number")
    return (rows[0].get("state"), rows[0].get("number")) if rows else (None, None)


_GOAL_TEXT_CACHE = {}


def report_text(path) -> str:
    """A seat's report as text, whatever codec the seat's shell wrote it in. A Windows seat
    once wrote valid JSON in the system code page and the strict UTF-8 read raised, so a
    finished shard counted as a crash. A BOM names UTF-16 or UTF-8; otherwise UTF-8, and a
    byte UTF-8 refuses falls back to the Windows code page, replacing what even that cannot
    read. A decode never raises; the JSON parse that follows judges the content."""
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _is_reparse_point(p: Path) -> bool:
    """True for a Windows junction/symlink or a POSIX symlink.

    os.path.islink() is FALSE for Windows directory junctions, which is exactly
    how the destructive case slipped through, the check must read the reparse
    tag, not the symlink bit.
    """
    try:
        st = p.lstat()
    except OSError:
        return False
    if os.name == "nt" and getattr(st, "st_reparse_tag", 0):
        return True
    return p.is_symlink()


def _unlink_reparse_points(root: Path) -> list:
    """Delete every junction/symlink under `root` AS A LINK, never recursing through
    it. Returns the paths unlinked, for the caller's report.

    Walked top-down with pruning: once a directory is identified as a reparse
    point it is removed and NOT descended into, so the target tree is never
    touched. os.rmdir() on a Windows junction unlinks it and leaves the target
    intact; os.unlink() handles file symlinks.
    """
    unlinked = []
    if not root.exists():
        return unlinked
    for dirpath, dirnames, filenames in os.walk(str(root), topdown=True):
        for name in list(dirnames):
            p = Path(dirpath) / name
            if _is_reparse_point(p):
                dirnames.remove(name)  # do NOT descend into the target
                try:
                    os.rmdir(p)        # unlinks the junction; target untouched
                    unlinked.append(str(p))
                except OSError:
                    try:
                        p.unlink()
                        unlinked.append(str(p))
                    except OSError:
                        pass
        for name in filenames:
            p = Path(dirpath) / name
            if _is_reparse_point(p):
                try:
                    p.unlink()
                    unlinked.append(str(p))
                except OSError:
                    pass
    return unlinked
