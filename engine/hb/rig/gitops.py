"""hb.run.gitops - a run's own tree and what it does with git.

One place for the git a run speaks: the command itself, the paths a branch changed, the
tip, the worktree a run is given, and the push, which is journaled so a resume never
pushes twice.
"""

import json
import time
from types import SimpleNamespace

from hb.rig.common import branch_setting, _P, run


def git(wt, *args, timeout=120):
    return run(["git", "-C", str(wt), *args], timeout=timeout)


# the committer name every seat's commits carry, so a commit in a seat's window that a seat
# did not make is told apart from the seat's own
SEAT_COMMITTER = "hb seat"


def foreign_commits(wt, tip_in, tip_out):
    """`sha subject` of each commit on the first-parent line from `tip_in` to `tip_out` that no
    seat made; a seat's merge brings others' commits in beside that line, never on it."""
    if not tip_in or not tip_out or tip_in == tip_out:
        return []
    rc, o, _ = git(wt, "log", "--first-parent", "--format=%cn%x09%h %s", f"{tip_in}..{tip_out}")
    if rc != 0:
        return []
    return [line.split("\t", 1)[1] for line in o.splitlines() if "\t" in line and line.split("\t", 1)[0] != SEAT_COMMITTER]


def changed_paths(wt, base_sha, to="HEAD"):
    rc, o, _ = git(wt, "diff", "--name-only", f"{base_sha}..{to}")
    return [l.strip() for l in o.splitlines() if l.strip()] if rc == 0 else []


def worktree(j, slug, root, c, shadow, branch_prefix="hotfix/", dir_prefix="run-", base_ref=None, prepare=None, key="worktree"):
    """The run's worktree, made once under `key`; `prepare(wt)` is the product's own preparation (hb.rig.provision)."""
    default = branch_setting(c, "landing")
    wt = root / _P("worktrees_dir") / f"{dir_prefix}{slug}"
    branch = f"{branch_prefix}{slug}"
    rc_r, remotes, _ = git(root, "remote")
    has_origin = rc_r == 0 and "origin" in remotes.split()
    # a checkout with no origin (a fresh clone of an example, a local experiment) branches off its own landing branch
    base_ref = base_ref or (f"origin/{default}" if has_origin else default)

    def check():
        if wt.exists() and (wt / ".git").exists():
            rc, sha, _ = git(wt, "rev-parse", "HEAD")
            if rc == 0:
                rc2, base, _ = git(root, "rev-parse", base_ref)
                return {"worktree": str(wt), "branch": branch, "base_sha": j.fact("base_sha") or base.strip()}
        return None

    def do():
        if has_origin:
            git(root, "fetch", "origin", "--prune", timeout=300)
        rc, base, e = git(root, "rev-parse", base_ref)
        if rc != 0:
            return {"error": f"{base_ref} unreadable: {e.strip()}"}
        base = base.strip()
        wt.parent.mkdir(parents=True, exist_ok=True)
        # An earlier run on the same slug may have left its tree and branch behind
        # (a crash before the first push). Reuse them: the branch is the state.
        if wt.exists() and (wt / ".git").exists():
            rc, cur, _ = git(wt, "rev-parse", "--abbrev-ref", "HEAD")
            if rc == 0 and cur.strip() == branch:
                return {"worktree": str(wt), "branch": branch, "base_sha": base, "reused": True}
            return {"error": f"{wt} exists on branch {cur.strip()!r}, not {branch}; remove it by hand"}
        # a wave starts every slice at once and each add writes .git/config: a loser of that
        # lock stopped its slice before any seat, so a lock refusal waits and tries again
        for attempt in range(6):
            rc_b, _, _ = git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
            if rc_b == 0:
                rc, o, e = git(root, "worktree", "add", str(wt), branch, timeout=600)
            else:
                rc, o, e = git(root, "worktree", "add", str(wt), "-b", branch, base_ref, timeout=600)
            # two adds at once also race on .git/worktrees/<name>/commondir ("failed to read ... commondir"), the
            # same contention the lock refusal is; both wait and try again
            racing = "lock" in e or "commondir" in e
            if rc == 0 or not racing or attempt == 5:
                break
            git(root, "worktree", "prune")
            time.sleep(2 + attempt * 3)
        if rc != 0:
            return {"error": f"git worktree add failed: {e.strip()[:300]}"}
        prov = None
        try:
            from hb.rig.provision import _provision_tree
            prov = _provision_tree(SimpleNamespace(issue=slug, wave="standalone"), c, root, wt, prepare=prepare)
            prov = json.loads(json.dumps(prov, default=str))
        except Exception as ex:  # provisioning is best effort; the gates measure the tree
            prov = {"error": f"{ex.__class__.__name__}: {ex}"}
        return {"worktree": str(wt), "branch": branch, "base_sha": base, "provisioned": prov}
    return j.op(key, do, check=check)


def start_from_studio(j, wt, studio, base_ref, default, base_sha):
    """Move a fresh slice branch onto the sitting's frozen studio commit, then merge the
    branch it would have started from when that moved past the studio. The studio commit
    must be on origin's studio branch and share history with the default branch. Returns
    {"base_sha": the new start} or {"error": ..., "conflict": [files]}."""
    sha, studio_branch = str(studio.get("commit")), str(studio.get("branch"))

    def do():
        git(wt, "fetch", "origin", studio_branch, default, timeout=300)
        rc, full, _ = git(wt, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
        full = full.strip()
        if rc != 0 or git(wt, "merge-base", "--is-ancestor", full, f"origin/{studio_branch}")[0] != 0:
            return {"error": f"the studio commit {sha} is not on origin/{studio_branch}"}
        if git(wt, "merge-base", full, f"origin/{default}")[0] != 0:
            return {"error": f"the studio commit {sha} shares no history with {default}; a studio branch is cut from {default}"}
        if in_history(wt, full):
            return {"base_sha": head_of(wt), "studio": full, "merged": False}
        if head_of(wt) != base_sha:
            return {"error": f"the slice branch carries commits past {base_sha[:9]} that are not on the studio commit {sha}"}
        git(wt, "reset", "-q", "--hard", full)
        merged = git(wt, "merge-base", "--is-ancestor", base_ref, "HEAD")[0] != 0
        if merged:
            rc, o, e = git(wt, "-c", "user.name=hb", "-c", "user.email=hb@local", "merge", "--no-edit", "-q", base_ref, timeout=300)
            if rc != 0:
                _, files, _ = git(wt, "diff", "--name-only", "--diff-filter=U")
                git(wt, "merge", "--abort")
                git(wt, "reset", "-q", "--hard", base_sha)
                return {"error": f"{base_ref} moved past the studio commit {sha[:9]} and does not merge into it",
                        "conflict": [f for f in files.splitlines() if f.strip()]}
        return {"base_sha": head_of(wt), "studio": full, "merged": merged}
    return j.op("studio-base", do)


def push(j, wt, branch, shadow, key):
    """A push is keyed by the tip it pushes, so a resume whose tree moved (a compose
    landed by hand, a fix committed between passes) pushes again instead of reading the
    old push as done."""
    if shadow:
        return {"skipped": "shadow"}
    return j.op(f"{key}@{head_of(wt)[:9]}",
                lambda: dict(zip(("rc", "out", "err"), git(wt, "push", "-u", "origin", branch, timeout=600))))


def sync_to_origin(wt, branch):
    """Bring a reused tree up to the branch origin holds: a repair re-enters the branch as it
    was pushed (a dev merge by hand after a sibling landed), not as the last run left the
    tree. Fast-forward only; a tree that diverged from origin is an error named, never a merge."""
    git(wt, "fetch", "origin", branch, timeout=300)
    rc, remote, _ = git(wt, "rev-parse", "--verify", "--quiet", f"origin/{branch}")
    if rc != 0:
        return {"moved": False, "why": "origin has no such branch"}
    before = head_of(wt)
    remote = remote.strip()
    if remote == before or git(wt, "merge-base", "--is-ancestor", remote, "HEAD")[0] == 0:
        return {"moved": False, "tip": before}
    rc, _, e = git(wt, "merge", "--ff-only", remote, timeout=300)
    if rc != 0:
        return {"moved": False, "error": f"the tree at {before[:9]} and origin/{branch} at {remote[:9]} diverged: {e.strip()[:200]}"}
    return {"moved": True, "from": before, "tip": head_of(wt)}


def head_of(cwd):
    rc, o, _ = run(["git", "-C", str(cwd), "rev-parse", "HEAD"], timeout=30)
    return o.strip() if rc == 0 else ""


def in_history(wt, sha):
    """True when `sha` is the tree's tip or an ancestor of it."""
    if not sha:
        return False
    return git(wt, "merge-base", "--is-ancestor", str(sha), "HEAD")[0] == 0
