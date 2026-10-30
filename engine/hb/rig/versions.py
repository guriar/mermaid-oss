"""hb.versions - the harness as frozen versions a run keeps.

A product repo holds no copy of the engine: the plugin is the install. A run started from a
plugin cache copies the engine it is about to use into the versions home
(`~/.mermaid/versions/<tree hash>/`) once and re-enters from there, so the plugin can update
under a run in flight and every seat the run spawns still reads the same code. A run started
from a git checkout (the drills, a developer) runs in place and reads `working`. A commit is
drilled once, wherever it is drilled, and the result is kept beside the versions as its stamp.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from hb.rig.common import now_iso

SHA_LEN = 12
ENV_HOME = "HB_VERSIONS_DIR"
# a process tree started from one version stays on it: the entry file reads this first
ENV_ENTRY = "HB_PINNED_ENTRY"


def hb_invocation():
    """The command line that runs this very engine: the pinned entry a run started from, else
    the engine's own hb.py. The engine prints it wherever it tells a session what to type, so
    no product path is ever written into the engine."""
    entry = os.environ.get(ENV_ENTRY) or str(Path(__file__).resolve().parents[2] / "hb.py")
    return f'"{sys.executable}" "{entry}"'


def harness_version():
    """The pinned version a run started on, or `working` from a checkout."""
    entry = os.environ.get(ENV_ENTRY)
    return Path(entry).resolve().parent.parent.name if entry else "working"


# the folders a frozen version carries: what a run and its seats read
FROZEN_PARTS = ("engine", "roles", "hooks", "templates", "commands")


def tree_root():
    """The root of the tree this engine runs from (the plugin root or the checkout)."""
    return Path(__file__).resolve().parents[3]


def is_checkout(root=None):
    return ((root or tree_root()) / ".git").exists()


def tree_hash(root=None):
    """A stable id of the engine's bytes: every file under the frozen parts, sorted, hashed."""
    root = root or tree_root()
    h = hashlib.sha256()
    for part in FROZEN_PARTS:
        for p in sorted((root / part).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                h.update(p.relative_to(root).as_posix().encode("utf-8"))
                h.update(p.read_bytes())
    return h.hexdigest()[:SHA_LEN]


def freeze(root=None):
    """Copy the frozen parts of the tree into the versions home once. Returns (entry, sha)."""
    root = root or tree_root()
    sha = tree_hash(root)
    dst = version_dir(sha)
    if not (dst / ".complete").exists():
        tmp = dst.with_name(dst.name + ".part")
        shutil.rmtree(tmp, ignore_errors=True)
        for part in FROZEN_PARTS:
            if (root / part).exists():
                shutil.copytree(root / part, tmp / part, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.rmtree(dst, ignore_errors=True)
        tmp.rename(dst)
        (dst / ".complete").write_text(f"{sha}\n", encoding="utf-8")
    return dst / "engine" / "hb.py", sha


def pin_self(opened_on=None):
    """The entry a run must continue from, or None to run in place.

    In place: already pinned (ENV_ENTRY set), or running from a git checkout. Otherwise the
    engine freezes itself; a resume prefers the version the run opened on when that version is
    still laid out, so a plugin updated mid-run never moves a run."""
    if os.environ.get(ENV_ENTRY) or is_checkout():
        return None
    if opened_on and opened_on != "working" and (version_dir(opened_on) / ".complete").exists():
        return version_dir(opened_on) / "engine" / "hb.py"
    entry, _sha = freeze()
    return entry


def reenter(entry, argv):
    """Run this same command from the frozen entry and return its exit code."""
    env = {**os.environ, ENV_ENTRY: str(entry)}
    # handed explicitly: on Windows a child given no handles does not inherit a captured pipe,
    # so the caller read an empty answer from a run that finished
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.call([sys.executable, str(entry), *argv], env=env, stdout=sys.stdout, stderr=sys.stderr)



def remove_tree(path):
    """Remove a tree whose git objects are read-only files, which a plain rmtree leaves behind on Windows."""
    def writable(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)
    shutil.rmtree(path, onerror=writable)


def home():
    return Path(os.environ.get(ENV_HOME) or Path.home() / ".mermaid")


def version_dir(sha):
    return home() / "versions" / str(sha)[:SHA_LEN]


def full_sha(src, ref):
    r = subprocess.run(["git", "-C", str(src), "rev-parse", f"{ref}^{{commit}}"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def drill_checkout(src, sha):
    """A throwaway git checkout of the commit for the drills to run in. They need a real
    repository (the install's own drill reads history), which a laid-out version is not.
    Returns (dir, problem); the caller removes it."""
    dst = home() / "drilling" / str(sha)[:SHA_LEN]
    if dst.exists():
        remove_tree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    for cmd in (["git", "clone", "-q", "--shared", "--no-checkout", str(src), str(dst)],
                ["git", "-C", str(dst), "checkout", "-q", "--detach", sha]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return None, f"{' '.join(cmd[:4])} failed: {(r.stderr or '')[:200]}"
    return dst, None


def stamp_path(sha):
    return home() / "drilled" / f"{str(sha)[:SHA_LEN]}.json"


def read_stamp(sha):
    try:
        return json.loads(stamp_path(sha).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_stamp(sha, green, rows):
    p = stamp_path(sha)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"commit": sha, "green": bool(green), "ts": now_iso(),
           "drills": [{"drill": r.get("drill"), "TRUE_EXIT": r.get("TRUE_EXIT"), "tail": str(r.get("tail") or "")[-200:]}
                      for r in rows]}
    p.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return doc
