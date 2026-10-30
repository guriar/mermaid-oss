"""hb.record.sittings - the sitting records, kept off this machine.

    hb sittings sync [--dry-run]

A sitting's record (the briefs, the rulings, the reviews, the bundle) lives under
`paths.sittings_dir`; the crops a contract pins live under `paths.design_dir` in the product
repo. When the sittings dir is its own git checkout (the record's home is a separate
repository), sync commits and pushes it. When it is a plain folder, sync mirrors every sitting
folder and the local design records from the main checkout and every worktree, the newer file
winning, into the private repository named by `sittings.backup_repo` ("owner/name") and pushes
that. A file that looks like it holds a secret, or is too large for a repository, is left out
and named in the answer, never pushed.
"""
import re
import shutil
import subprocess
from pathlib import Path

from hb.rig import versions
from hb.rig.common import _P, canon, cfg, now_iso, out

MAX_MB = 20
SECRET_NAME = re.compile(r"(^|[\\/])(\.env(\..*)?|.*\.pem|.*\.key|.*credential.*|.*secret.*|.*token.*)$", re.I)
# shaped like a real key, never a word that happens to hold "sk-" or picture data that spells AKIA
SECRET_TEXT = re.compile(
    r"((?<![A-Za-z0-9-])sk-(?:ant-|proj-)?[A-Za-z0-9_]{32,}|(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|(?<![A-Za-z0-9+/])AKIA[0-9A-Z]{16}(?![A-Za-z0-9+/])|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?<![A-Za-z0-9+/_-])eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"
    r"|(?i:client_secret|api_key|apikey|password|service_role)[\"']?\s*[:=]\s*[\"'][^\"'\s<>{}$]{16,}[\"'])")
TEXT_SUFFIX = (".md", ".txt", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".html", ".css", ".csv", ".sh", ".toml")


def _git(cwd, *args, timeout=600):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def sources(root):
    """(label, folder) for every place sitting records live: the main checkout and each worktree."""
    trees = [Path(root)]
    rc, o, _ = _git(root, "worktree", "list", "--porcelain")
    trees += [Path(line[9:]) for line in o.splitlines() if line.startswith("worktree ") and Path(line[9:]) != Path(root)]
    found = []
    for t in trees:
        for label, rel in (("sittings", _P("sittings_dir")), ("design", _P("design_dir"))):
            if (t / rel).is_dir():
                found.append((label, t / rel))
    return found


def withheld(path):
    """Why this file is not copied, or None. A name or a line that looks like a secret keeps
    the whole file out; so does a size no repository should carry."""
    if SECRET_NAME.search(str(path)):
        return "its name looks like a secret"
    try:
        size = path.stat().st_size
    except OSError:
        return "unreadable"
    if size > MAX_MB * 1024 * 1024:
        return f"over {MAX_MB} MB"
    if path.suffix.lower() in TEXT_SUFFIX:
        try:
            if SECRET_TEXT.search(path.read_text(encoding="utf-8", errors="replace")):
                return "a line in it looks like a secret"
        except OSError:
            return "unreadable"
    return None


def plan(root):
    """{relative path: source file} with the newest copy of each file, and what was left out."""
    newest, left = {}, []
    for label, folder in sources(root):
        for p in folder.rglob("*"):
            if not p.is_file() or ".git" in p.parts or "node_modules" in p.parts:
                continue
            rel = f"{label}/{p.relative_to(folder).as_posix()}"
            why = withheld(p)
            if why:
                left.append({"file": rel, "why": why})
                continue
            if rel not in newest or p.stat().st_mtime > newest[rel].stat().st_mtime:
                newest[rel] = p
    return newest, [x for i, x in enumerate(left) if x not in left[:i]]


def _sync_checkout(home, dry_run):
    """The sittings dir is its own repository: commit what changed there and push it."""
    _git(home, "add", "-A")
    rc, status, _ = _git(home, "status", "--porcelain")
    changed = len([l for l in status.splitlines() if l.strip()])
    if dry_run:
        out({"verdict": "dry-run", "home": str(home), "changed": changed}, 0)
    if not changed:
        rc, _, e = _git(home, "push", "-q")
        out({"verdict": "current", "home": str(home), "pushed": rc == 0, **({"why": e[:200]} if rc else {})}, 0)
    _git(home, "-c", "user.name=hb", "-c", "user.email=hb@local", "commit", "-q", "-m", f"sitting records {now_iso()[:16]}: {changed} files")
    rc, _, e = _git(home, "push", "-q")
    if rc != 0:
        out({"verdict": "not-pushed", "why": e[:300], "home": str(home), "changed": changed}, 1)
    out({"verdict": "synced", "home": str(home), "changed": changed}, 0)


def cmd_sittings(a):
    c = cfg()
    home = canon() / _P("sittings_dir")
    if (home / ".git").exists():
        _sync_checkout(home, getattr(a, "dry_run", False))
    repo = (c.get("sittings") or {}).get("backup_repo")
    if not repo:
        out({"verdict": "no-repo", "why": f"{home} is not a git checkout and sittings.backup_repo is not set in harness-config.json"}, 2)
    files, left = plan(canon())
    clone = versions.home() / "sittings" / repo.replace("/", "__")
    if getattr(a, "dry_run", False):
        out({"verdict": "dry-run", "repo": repo, "files": len(files), "left_out": left[:60], "clone": str(clone)}, 0)
    if not (clone / ".git").is_dir():
        clone.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["gh", "repo", "clone", repo, str(clone)], capture_output=True, text=True)
        if r.returncode != 0:
            out({"verdict": "no-clone", "why": (r.stderr or "")[:300], "repo": repo}, 2)
    else:
        _git(clone, "pull", "-q", "--rebase")
    copied = 0
    for rel, src in files.items():
        dst = clone / rel
        if dst.exists() and dst.stat().st_size == src.stat().st_size and dst.stat().st_mtime >= src.stat().st_mtime:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    _git(clone, "add", "-A")
    rc, status, _ = _git(clone, "status", "--porcelain")
    if not status:
        out({"verdict": "current", "repo": repo, "files": len(files), "left_out": left[:60]}, 0)
    _git(clone, "-c", "user.name=hb", "-c", "user.email=hb@local", "commit", "-q", "-m",
         f"sitting records {now_iso()[:16]}: {copied} files")
    rc, _, e = _git(clone, "push", "-q", "origin", "HEAD")
    if rc != 0:
        out({"verdict": "not-pushed", "why": e[:300], "repo": repo, "copied": copied}, 1)
    out({"verdict": "synced", "mirror": repo, "files": len(files), "copied": copied, "left_out": left[:60]}, 0)


def register(sub):
    s = sub.add_parser("sittings", help="sync the sitting records: push their own checkout, or mirror them to the private repository in the config")
    s.add_argument("op", choices=("sync",))
    s.add_argument("--dry-run", dest="dry_run", action="store_true")
