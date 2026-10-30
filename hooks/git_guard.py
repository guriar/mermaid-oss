#!/usr/bin/env python
"""PreToolUse hook: destructive-git guard and the landing wall.

Reads the hook JSON on stdin. Exit 0 = allow, exit 2 = block (stderr = reason).
Only inspects Bash tool calls. Two walls:

  1. Destructive git anywhere in the command: push --force/-f/--force-with-lease,
     reset --hard, clean -f*, branch -D / --delete --force, and ref DELETION by
     push (`push origin --delete X`, `push origin :X`).

     v9.2 W carves ONE hole in the deletion half of that wall, and only there:
     `git branch -D <name>` and `git push origin --delete <name>` are ALLOWED
     when EVERY branch the command names lives in a harness-owned namespace
     (`_DELETABLE_BRANCH_RE`: slice/ build/ fix/ verify/ design/ review/ here/
     _land- worktree-agent- claude/) and is not the current branch. Those refs are
     litter the harness itself creates by the hundred, and a guard that refuses
     the cleanup is a guard that gets routed around. `wave/`, `dev`, `main`,
     `docs/`, `hotfix/` and `harness/` stay protected, deletion included; force
     PUSH (rewriting a ref) is untouched by this and stays blocked everywhere.
  2. The landing wall: a push targeting `dev`, `main` or `master` is refused from any
     process. The harness lands through PRs and merges by gh; prod promotes via the
     owner's dev->main PR.
  3. Promotion wall (owner-ruled): the PR lane to main is walled
     too: `gh pr create/edit --base main`, `gh api` pull-creation with
     base=main, and `gh pr merge` of a main-based PR are ALL blocked unless a
     fresh promotion marker (.context/harness/promotion-armed.json, <90 min)
     exists. Only the owner-invoked promotion door writes that marker,
     after an explicit in-chat confirm. Waves and slices can never promote.

The DESTRUCTIVE gate and the landing wall have no fail-open: if we can see the pattern,
we block.
One rule, one home: the prose versions of these rules were deleted from the
command files when this hook landed (dev-pipeline-v6.3.md R2).
"""
import json
import os
import re
import shlex
import subprocess
import sys


def _tokens(cmd):
    """Quote-aware tokens with shell separators as tokens OF THEIR OWN.

    Plain `shlex.split` leaves a separator GLUED to
    the token before it, so `git push origin wave/records-refound; echo
    TRUE_EXIT=$?` tokenized the branch as `wave/records-refound;`, no live claim
    has that slug, and the hook refused a legitimate landing push. That collides
    head-on with the gate rule that MANDATES appending `; echo TRUE_EXIT=$?` to every
    gate command, so the two rules made each other unusable.

    `punctuation_chars=True` splits `;`, `|`, `&`, `(`, `)` out as separate tokens
    while still honouring quotes: `git commit -m "git reset --hard"` stays ONE
    opaque token, so the original false positive this function was written for
    does not come back. It also closes the second fail-open edge the old docstring
    named (`a;git`). `bash -c "..."` bodies remain opaque, deliberately.
    """
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return cmd.split()


def _segments(cmd):
    """Split a shell command into pipeline/logic segments, tokenized."""
    toks = _tokens(cmd)
    segs, cur = [], []
    skip = False
    for t in toks:
        if skip:  # the file a redirection names is not an argument of the command
            skip = False
            continue
        if t in ("&&", "||", ";", ";;", "|", "&", "(", ")", "\n"):
            if cur:
                segs.append(cur)
                cur = []
        elif t and set(t) <= set("<>&"):
            # a redirection: `2>&1` tokenizes as `2`, `>&`, `1`; neither the fd nor the target is a branch
            if cur and cur[-1].isdigit():
                cur.pop()
            skip = True
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def _git_call(toks):
    """If this segment is a git call, return (subcommand, args-after-subcommand)."""
    if "git" not in toks:
        return None, []
    i = toks.index("git") + 1
    # skip global flags like -C <path>, -c k=v, --no-pager
    while i < len(toks):
        t = toks[i]
        if t in ("-C", "-c"):
            i += 2
        elif t.startswith("-"):
            i += 1
        else:
            return t, toks[i + 1:]
    return None, []


def _push_targets(args, cwd):
    """Branch names a `git push` lands on. Resolves bare pushes via the checkout."""
    refs = []
    positionals = [a for a in args if not a.startswith("-")]
    # positionals[0] is the remote (if present); the rest are refspecs
    for spec in positionals[1:]:
        refs.append(spec.split(":", 1)[1] if ":" in spec else spec)
    if not refs:
        try:
            r = subprocess.run(
                ["git", "-C", cwd or ".", "branch", "--show-current"],
                capture_output=True, text=True, timeout=10,
            )
            cur = r.stdout.strip()
            if cur:
                refs.append(cur)
        except Exception:
            pass  # an unresolvable bare push names no ref here
    return refs


# v9.2 W - the namespaces the harness OWNS and may delete. hb's own branch GC
# (engine/hb/worktrees.py) reads this one from here.
_DELETABLE_BRANCH_RE = re.compile(
    r"^(slice|build|fix|verify|design|review|here/|_land-|worktree-agent-|claude/)")


def _current_branch(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd or ".", "branch", "--show-current"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return ""


def _deletable(names, cwd):
    """(ok, offenders) - may this command delete EVERY ref it names?

    All-or-nothing on purpose: one protected name in a list makes the whole
    command a refusal, so a `wave/x` cannot ride along behind three slice refs.
    """
    cur = _current_branch(cwd)
    bad = [n for n in names
           if not _DELETABLE_BRANCH_RE.match(n) or (cur and n == cur)]
    return (bool(names) and not bad), bad


def _block(reason):
    sys.stderr.write(reason + "\n")
    sys.exit(2)


def _promotion_armed():
    """Fresh owner-armed promotion marker, or None. Written ONLY by /promote
    after the owner's explicit confirm; expires after 90 minutes."""
    import time
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    p = os.path.join(root, ".context", "harness", "promotion-armed.json")
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        if time.time() - float(m.get("armed_at_epoch", 0)) < 90 * 60:
            return m
    except Exception:
        pass
    return None


def _gh_pr_base(selector, cwd):
    """Resolve a PR's base branch via gh. None on any failure (fail-open:
    a gh outage must not brick the wave's routine dev-based merges)."""
    argv = ["gh", "pr", "view"] + ([selector] if selector else []) + \
           ["--json", "baseRefName", "-q", ".baseRefName"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=20,
                           cwd=cwd or None)
        base = r.stdout.strip()
        return base or None
    except Exception:
        return None


_PROMO_MSG = ("git-guard: the PR lane to main is owner-only (promotion wall). "
              "Promotion happens ONLY through the owner-invoked "
              "promotion door, which arms a short-lived marker after an "
              "explicit confirm. Nothing a wave or slice does may target main.")


def _check_gh(toks, cwd):
    """Promotion wall over gh: pr create/edit --base main, api pull-creation
    with base=main, pr merge of a main-based PR."""
    if "gh" not in toks:
        return
    i = toks.index("gh")
    rest = toks[i + 1:]
    if not rest:
        return
    joined = " ".join(rest)

    def base_is_main():
        for j, t in enumerate(rest):
            if t in ("--base", "-B") and j + 1 < len(rest) and rest[j + 1] in ("main", "master"):
                return True
            if t in ("--base=main", "-B=main", "--base=master"):
                return True
        return False

    if rest[0] == "pr" and len(rest) > 1 and rest[1] in ("create", "edit") and base_is_main():
        if not _promotion_armed():
            _block(_PROMO_MSG)
    elif rest[0] == "api" and re.search(r'base["\']?\s*[:=]\s*["\']?(main|master)\b', joined) \
            and ("pulls" in joined or "merge" in joined):
        if not _promotion_armed():
            _block(_PROMO_MSG)
    elif rest[0] == "pr" and len(rest) > 1 and rest[1] == "merge":
        positionals = [t for t in rest[2:] if not t.startswith("-")]
        base = _gh_pr_base(positionals[0] if positionals else None, cwd)
        if base in ("main", "master"):
            armed = _promotion_armed()
            if not armed:
                _block(_PROMO_MSG)
    elif re.search(r"pulls/\d+/merge", joined):
        m = re.search(r"pulls/(\d+)/merge", joined)
        base = _gh_pr_base(m.group(1), cwd)
        if base in ("main", "master") and not _promotion_armed():
            _block(_PROMO_MSG)


def _msys_to_win(path):
    """Git Bash spells C:/x as /c/x; git.exe launched from Python does not read that."""
    m = re.match(r"^/([a-zA-Z])/(.*)$", path or "")
    return f"{m.group(1).upper()}:/{m.group(2)}" if m else path


def _common_dir(path):
    """Absolute git common dir of the checkout at `path` (worktrees share it), or None."""
    path = _msys_to_win(path)
    try:
        r = subprocess.run(["git", "-C", path or ".", "rev-parse", "--path-format=absolute",
                            "--git-common-dir"], capture_output=True, text=True, timeout=10)
        out = r.stdout.strip()
        return os.path.normcase(os.path.abspath(out)) if r.returncode == 0 and out else None
    except Exception:
        return None


def _is_this_repo(path):
    """True when the git call at `path` targets the guarded project (canon or one of its
    worktrees). A sibling repo on the same machine gets no branch-name rules: those rules
    describe THIS repo's landing branches. Unresolvable = True (fail closed on the gate)."""
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    mine, theirs = _common_dir(root), _common_dir(path)
    if mine is None or theirs is None:
        return True
    return mine == theirs


def _resolve_dir(toks, cur_dir):
    """The directory a git segment runs in: an explicit -C wins, else the cd-tracked dir."""
    for i, t in enumerate(toks):
        if t == "-C" and i + 1 < len(toks):
            d = toks[i + 1]
            return d if os.path.isabs(d) else os.path.join(cur_dir or ".", d)
        if t.startswith("-C") and len(t) > 2:
            d = t[2:]
            return d if os.path.isabs(d) else os.path.join(cur_dir or ".", d)
    return cur_dir


def _under_a_harness(cwd):
    """Whether `cwd` or an ancestor carries .claude/harness-config.json: the only trees the guard owns."""
    d = os.path.abspath(cwd or os.getcwd())
    while True:
        if os.path.isfile(os.path.join(d, ".claude", "harness-config.json")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if payload.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if "git" not in cmd and not re.search(r"\bgh\b", cmd):
        sys.exit(0)
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or ""
    if not _under_a_harness(cwd):
        sys.exit(0)  # a repo with no harness config has no landing wall and no branch rules

    cur_dir = cwd
    for toks in _segments(cmd):
        if toks and toks[0] == "cd" and len(toks) > 1:
            d = toks[1]
            cur_dir = d if os.path.isabs(d) else os.path.join(cur_dir or ".", d)
            continue
        _check_gh(toks, cur_dir)
        sub, args = _git_call(toks)
        if not sub:
            continue

        if sub == "push":
            bad = [a for a in args if a == "-f" or a.startswith("--force")]
            if bad:
                _block(f"git-guard: force push blocked ({' '.join(bad)}). "
                       "The branch is the asset; never rewrite a shared ref.")
            seg_dir = _resolve_dir(toks, cur_dir)
            # v9.2 W - ref DELETION by push is its own gate, decided before the
            # landing/claim gates below: a delete is not a landing, and the claim
            # a wave holds licenses pushing TO its branch, never removing it.
            positionals = [x for x in args if not x.startswith("-")]
            delete_mode = ("--delete" in args or "-d" in args
                           or any(x.startswith(":") for x in positionals[1:]))
            if delete_mode:
                named = [x.split(":", 1)[1] if ":" in x else x
                         for x in positionals[1:]]
                named = [n for n in named if n]
                ok, offenders = _deletable(named, seg_dir)
                if not ok:
                    _block("git-guard: remote branch deletion is limited to the "
                           "harness-owned namespaces (slice/ build/ fix/ verify/ "
                           "design/ review/ here/ _land- worktree-agent- claude/), and "
                           "never the current branch. Refused for: "
                           + (", ".join(offenders) or "(no branch named)")
                           + ". wave/, dev, main, docs/, hotfix/ and harness/ are "
                             "protected refs; delete one by hand if you truly mean it.")
                continue  # allowed deletion: the landing gates do not apply
            if not _is_this_repo(seg_dir):
                continue  # a sibling repo: its branch names mean nothing here
            targets = _push_targets(args, seg_dir)
            walled = [t for t in targets if t in ("dev", "main", "master")]
            if walled:
                _block(f"git-guard: no push to {walled[0]} from any process. The harness lands "
                       "through a PR and gh merges it; prod promotes via the owner's dev->main PR.")

        elif sub in ("commit", "merge", "cherry-pick", "revert", "rebase") and not os.environ.get("HB_SEAT_ROLE"):
            # the runner is the sole integrator of a wave branch; a seat it sat carries HB_SEAT_ROLE
            seg_dir = _resolve_dir(toks, cur_dir)
            cur = _current_branch(seg_dir)
            if cur.startswith("wave/") and _is_this_repo(seg_dir):
                _block(f"git-guard: no hand commit on {cur}; the runner is the sole integrator of a wave branch. "
                       f"Hand it a brief: hb run wave {cur[len('wave/'):]} --resume <run id> --brief \"<what to build>\"; "
                       "it enters as a build seat at the next loop boundary, and the next read reviews it.")

        elif sub == "reset" and "--hard" in args:
            _block("git-guard: `git reset --hard` is blocked everywhere in the harness.")

        elif sub == "clean":
            if any(re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*", a) for a in args) or "--force" in args:
                _block("git-guard: `git clean -f` is blocked.")

        elif sub == "branch":
            if "-D" in args or ("--delete" in args and "--force" in args):
                # v9.2 W: allowed inside the harness-owned namespaces, never for a
                # protected ref and never for the branch you are standing on.
                named = [x for x in args if not x.startswith("-")]
                ok, offenders = _deletable(named, _resolve_dir(toks, cur_dir))
                if not ok:
                    _block("git-guard: force branch deletion is limited to the "
                           "harness-owned namespaces (slice/ build/ fix/ verify/ "
                           "design/ review/ here/ _land- worktree-agent- claude/), and "
                           "never the current branch. Refused for: "
                           + (", ".join(offenders) or "(no branch named)")
                           + ". For anything else use `git branch -d`, which "
                             "refuses to lose unmerged work.")

    sys.exit(0)


if __name__ == "__main__":
    main()
