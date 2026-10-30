"""hb.worktrees - sweeping what finished runs leave: landed worktrees, landed branches, stray stand-ins."""

import functools
import json
from pathlib import Path

from hb.rig.common import branch_setting, canon, cfg, die, gh, out, _P, _pr_state_for_head, run, state_dir, _unlink_reparse_points


def protected_branches(c):
    """The branches no sweep ever deletes: the landing branch and the promotion base."""
    return {branch_setting(c, "landing"), branch_setting(c, "promotion_base")}


def cmd_branch_gc(a):
    """Branch hygiene (2026-08-03). Squash-merge landings mean `git branch --merged`
    is structurally blind, the ONLY honest merged-signal is GitHub's PR state.
    Removable: (a) remote branch is the head of a MERGED or CLOSED PR (v9.2 X: the
    bulk merged list plus a capped per-branch `--head` probe for what it missed);
    (b) no PR but the tip is an ancestor of origin/<default> (fast-forward relics). Never
    touched: the protected branches, anything with an OPEN PR, anything checked out
    in a live worktree. Local branches: removable when their tip is on origin or an
    ancestor of origin/<default> (nothing unpushed dies). Dry-run by default."""
    c = cfg()
    default = branch_setting(c, "landing")
    protected = protected_branches(c)
    run(["git", "fetch", "--prune", "origin"], timeout=120)
    rc, o, e = gh(["pr", "list", "--state", "all", "--limit", "1000",
                   "--json", "headRefName,state", "--repo", c["repo"]], timeout=120)
    if rc != 0:
        die(f"gh pr list failed, branch-gc needs PR state to be safe: {e[:200]}")
    pr_state = {}
    # v9.2 X: the bulk merged list is the cheap half of the squash-blind signal;
    # `_pr_head_merged` is the per-branch `--head` probe for what the paged bulk
    # listing missed, capped so a 900-branch estate cannot spend the afternoon.
    merged_heads, merged_why = _merged_pr_heads(c)
    probe_budget = [_HEAD_PROBE_CAP]
    for p in json.loads(o):
        # OPEN wins over anything else recorded for the same ref name
        prev = pr_state.get(p["headRefName"])
        pr_state[p["headRefName"]] = "OPEN" if "OPEN" in (prev, p["state"]) else p["state"]
    wt_branches = set()
    rc, o, _ = run(["git", "worktree", "list", "--porcelain"])
    for ln in (o or "").splitlines():
        if ln.startswith("branch "):
            wt_branches.add(ln.split(" ", 1)[1].strip().removeprefix("refs/heads/"))
    # a LIVE wave's branch is never a candidate, whatever the graph says. A wave branch sat
    # at the landing branch's exact tip before its launch (a fresh wave carries no commits),
    # so the ancestor test read it as merged and a wave-close deleted it local, remote and worktree.
    live_wave_branches = {f"{branch_setting(c, 'integration_prefix')}{w}" for w in _live_waves(c)}
    # A studio a filed contract names is where its slices start; a substrate's closeout
    # once swept a studio branch and the wave behind it could not launch.
    contract_studios = _studios_contracts_name(default)
    rc, o, _ = run(["git", "for-each-ref", "--format=%(refname:short)|%(objectname)",
                    "refs/remotes/origin"], timeout=60)
    remote_del, remote_keep = [], {}
    for ln in (o or "").splitlines():
        name, _, sha = ln.partition("|")
        short = name.removeprefix("origin/")
        if short in protected or short == "HEAD":
            continue
        if short in live_wave_branches:
            remote_keep[short] = "LIVE wave (its run is working); never gc'd"
            continue
        if short in contract_studios:
            remote_keep[short] = "a filed contract starts from this studio; never gc'd"
            continue
        st = pr_state.get(short) or ("MERGED" if short in merged_heads else None)
        if st == "OPEN":
            remote_keep[short] = "open PR"
        elif st in ("MERGED", "CLOSED"):
            remote_del.append(short)
        elif run(["git", "merge-base", "--is-ancestor", sha.strip(),
                  f"origin/{default}"], timeout=30)[0] == 0:
            remote_del.append(short)
        elif _pr_head_merged(c, short, probe_budget):
            remote_del.append(short)   # v9.2 X: MERGED PR, ancestry blind to it
        else:
            remote_keep[short] = "no PR, tip not on " + default
    rc, o, _ = run(["git", "for-each-ref", "--format=%(refname:short)|%(objectname)",
                    "refs/heads"], timeout=60)
    local_del, local_keep = [], {}
    cur = run(["git", "branch", "--show-current"])[1].strip()
    for ln in (o or "").splitlines():
        name, _, sha = ln.partition("|")
        if name in protected or name == cur:
            continue
        if name in wt_branches:
            local_keep[name] = "checked out in a worktree"
            continue
        if name in live_wave_branches:
            local_keep[name] = "LIVE wave (its run is working); never gc'd"
            continue
        if name in contract_studios:
            local_keep[name] = "a filed contract starts from this studio; never gc'd"
            continue
        sha = sha.strip()
        on_origin = run(["git", "branch", "-r", "--contains", sha], timeout=30)
        if (on_origin[0] == 0 and on_origin[1].strip()) or run(
                ["git", "merge-base", "--is-ancestor", sha, f"origin/{default}"],
                timeout=30)[0] == 0:
            local_del.append((name, False))
        elif ((name in merged_heads or _pr_head_merged(c, name, probe_budget))
              and _guard_regex().match(name)):
            # v9.2 X: squashed into its PR, so its tip is an ancestor of nothing.
            # -D is the only delete that works, and it is licensed ONLY inside the
            # namespaces the harness owns (the same list the git-guard allows).
            local_del.append((name, True))
        else:
            local_keep[name] = "tip not pushed anywhere (unpushed work)"
    if not a.apply:
        out({"mode": "dry-run", "merged_pr_heads": merged_why,
             "force_deletable_locals": [n for n, f in local_del if f],
             "remote_removable": len(remote_del),
             "remote_kept": len(remote_keep), "local_removable": len(local_del),
             "local_kept": len(local_keep),
             "remote_kept_reasons": dict(sorted(remote_keep.items())[:20]),
             "local_kept_reasons": dict(sorted(local_keep.items())[:20]),
             "note": "re-run with --apply to delete; deletion uses git push --delete "
                     "(the git-guard hook does not gate deletions of non-protected refs)"})
    deleted_r = deleted_l = errs = 0
    for chunk in [remote_del[i:i + 20] for i in range(0, len(remote_del), 20)]:
        rc, _, e = run(["git", "push", "origin", "--delete"] + chunk, timeout=300)
        if rc == 0:
            deleted_r += len(chunk)
        else:
            errs += 1
    forced = 0
    refused = []
    for name, force in local_del:
        # Every name here is proven landed: its tip is on origin, or its PR is MERGED and
        # its name is in a harness-owned namespace. `-d` asks the local checkout instead,
        # and a checkout behind origin refused every one in silence, so the proof decides.
        rc, _, e = run(["git", "branch", "-D", name], timeout=30)
        deleted_l += 1 if rc == 0 else 0
        forced += 1 if (rc == 0 and force) else 0
        if rc != 0:
            refused.append(f"{name}: {(e or '').strip()[:120]}")
    out({"mode": "applied", "remote_deleted": deleted_r, "local_deleted": deleted_l,
         "local_force_deleted": forced, "merged_pr_heads": merged_why,
         "push_errors": errs, "remote_kept": len(remote_keep), "local_kept": len(local_keep),
         "errors": refused + ([f"{errs} remote delete(s) refused"] if errs else [])})


def _studios_contracts_name(default):
    """Every studio branch a contract manifest on origin/<default> names (``studio.branch``).
    ponytail: keeps a studio as long as any manifest names it; prune by hand once its waves land."""
    rc, o, _ = run(["git", "ls-tree", "--name-only", f"origin/{default}", _P("manifest_dir").rstrip("/") + "/"], timeout=30)
    studios = set()
    for path in (o or "").splitlines():
        if not path.endswith(".json"):
            continue
        rc, text, _ = run(["git", "show", f"origin/{default}:{path}"], timeout=30)
        try:
            branch = ((json.loads(text) or {}).get("studio") or {}).get("branch")
        except (ValueError, AttributeError):
            continue
        if branch:
            studios.add(branch)
    return studios


def _live_waves(c):
    """The waves a run is still working on, read from each run's own phase view: a wave branch
    sitting at the landing branch's tip before its first commit is not landed while its run is live."""
    from hb.record.journal import FINAL
    live = set()
    runs = state_dir(c) / "runs"
    for pp in runs.glob("*/phase.json") if runs.exists() else []:
        try:
            ph = json.loads(pp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if ph.get("beat") == "wave" and ph.get("ref") and ph.get("phase") not in FINAL:
            live.add(str(ph["ref"]))
    return live


@functools.lru_cache(maxsize=1)
def _guard_regex():
    """The guard's `_DELETABLE_BRANCH_RE`, loaded from the guard's own file when asked."""
    import importlib.util
    path = Path(__file__).resolve().parents[3] / "hooks" / "git_guard.py"
    spec = importlib.util.spec_from_file_location("hb_git_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._DELETABLE_BRANCH_RE


# Per-branch `gh pr list --head` probes are ONE http call each. The bulk merged
# list answers almost everything; this caps what the fallback may spend.
_HEAD_PROBE_CAP = 60


def _merged_pr_heads(c, limit=1000):
    """Head branch names of MERGED PRs, in ONE gh call (v9.2 X).

    Squash landing makes ancestry structurally blind: a slice branch squashed
    into a wave branch is an ancestor of nothing, so `merge-base --is-ancestor`
    keeps it forever. Returns (set, why). gh failure yields an EMPTY set with an
    UNMEASURED why - never read as "nothing is merged".
    """
    rc, o, e = gh(["pr", "list", "--repo", c["repo"], "--state", "merged",
                   "--limit", str(limit), "--json", "headRefName"], timeout=180)
    if rc != 0:
        return set(), f"UNMEASURED: gh pr list --state merged failed ({e[:160]})"
    try:
        rows = json.loads(o or "[]")
    except ValueError:
        return set(), "UNMEASURED: gh pr list --state merged returned unparseable JSON"
    return ({r.get("headRefName") for r in rows if r.get("headRefName")},
            f"{len(rows)} merged PR head(s) from gh")


def _pr_head_merged(c, branch, budget):
    """v9.2 X, the per-branch fallback: is `branch` the head of a MERGED PR?
    Spends one probe from `budget` ([int], mutated); False once it is empty."""
    if not branch or budget[0] <= 0:
        return False
    budget[0] -= 1
    return _pr_state_for_head(c, branch)[0] == "MERGED"


def cmd_worktree_gc(a):
    """Sweep worktrees whose branch is fully merged into origin/<default>, or whose PR merged.

    DRY-RUN by default, prints candidates and keep-reasons; --apply removes.
    Never touches: the primary checkout, the current working directory, a live wave's
    worktree, and any DIRTY tree (unattributed edits are a hard-stop class, never a deletion).
    """
    c = cfg()
    root = canon().resolve()
    cwd = Path.cwd().resolve()
    run(["git", "fetch", "origin", "--prune"], timeout=180)
    rc, o, e = run(["git", "worktree", "list", "--porcelain"], timeout=60)
    if rc != 0:
        die(f"git worktree list failed: {e}")
    entries, cur = [], {}
    for ln in o.splitlines() + [""]:
        if ln.startswith("worktree "):
            cur = {"path": ln[len("worktree "):].strip()}
        elif ln.startswith("branch "):
            cur["branch"] = ln[len("branch "):].strip().removeprefix("refs/heads/")
        elif ln.startswith("detached"):
            cur["branch"] = None
        elif not ln.strip() and cur:
            entries.append(cur)
            cur = {}
    default = "origin/" + branch_setting(c, "landing")
    # a live wave's worktree is protected
    protected = {f"{branch_setting(c, 'integration_prefix')}{w_}" for w_ in _live_waves(c)}
    # v9.2 X: ancestry is squash-blind. A branch whose PR is MERGED has landed
    # whatever the graph says, so the PR state is a SECOND merged-signal here.
    merged_heads, merged_why = _merged_pr_heads(c)

    def _landed_why(br):
        """Why `br`'s content is already on the default branch, or None."""
        if not br:
            return None
        if br in merged_heads:
            return "its PR is MERGED per gh (squash-blind signal, v9.2 X)"
        return None

    keep, candidates = [], []
    for w in entries:
        p = Path(w["path"])
        rp = p.resolve() if p.exists() else p
        br = w.get("branch")
        if rp == root:
            keep.append({**w, "why": "primary checkout"})
        elif rp == cwd or cwd.is_relative_to(rp):
            keep.append({**w, "why": "current working directory"})
        elif not p.exists():
            candidates.append({**w, "why": "path gone, prune record"})
        elif br in protected:
            keep.append({**w, "why": f"a live wave's run works on {br}"})
        else:
            rc2, o2, _ = run(["git", "-C", str(p), "status", "--porcelain"], timeout=90)
            lines = [ln for ln in o2.splitlines() if ln.strip()]
            untracked_only = bool(lines) and all(ln.startswith("??") for ln in lines)
            if rc2 != 0:
                keep.append({**w, "why": "git status failed, inspect manually"})
            elif lines and not untracked_only:
                keep.append({**w, "why": "DIRTY tree (modified tracked files), never auto-delete"})
            elif untracked_only and not a.include_untracked:
                keep.append({**w, "why": f"untracked files only ({len(lines)}), sweepable with --include-untracked"})
            elif untracked_only and br and not _landed_why(br) and run(["git", "merge-base", "--is-ancestor", br, default], timeout=60)[0] != 0:
                keep.append({**w, "why": f"{br} has unmerged commits"})
            elif untracked_only:
                lw = _landed_why(br)
                why_tail = (f"{lw}, force-remove" if lw
                            else f"{br or 'detached'} merged/no branch, force-remove")
                candidates.append({**w, "why": f"untracked-scratch only ({len(lines)} paths), {why_tail}",
                                   "wave_landed": bool(lw)})
            elif br is None:
                candidates.append({**w, "why": "clean detached HEAD"})
            elif _landed_why(br):
                candidates.append({**w, "why": f"clean, {_landed_why(br)}",
                                   "wave_landed": True})
            else:
                rc3, _, _ = run(["git", "merge-base", "--is-ancestor", br, default], timeout=60)
                if rc3 == 0:
                    candidates.append({**w, "why": f"clean, {br} fully merged into {default}"})
                else:
                    keep.append({**w, "why": f"{br} has unmerged commits"})
    removed, errors = [], []
    if a.apply:
        for cand in candidates:
            # the owner's ruling: NEVER let the remove recurse through a junction or symlink.
            # A worktree may hold a shared dependency folder as a JUNCTION into another tree;
            # `git worktree remove` followed one and emptied its TARGET, destroying the host's
            # only working copy while the worktree itself survived.
            # Deleting the link FIRST makes the subsequent tree-delete unable to reach it.
            unlinked = _unlink_reparse_points(Path(cand["path"]))
            if unlinked:
                cand["unlinked_reparse_points"] = unlinked
            args = ["git", "worktree", "remove"]
            if "force-remove" in cand["why"]:  # untracked scratch blocks plain remove
                args.append("--force")
            rc4, _, e4 = run(args + [cand["path"]], timeout=180)
            if rc4 != 0:
                errors.append({"path": cand["path"], "error": e4})
                continue
            removed.append(cand["path"])
            br = cand.get("branch")
            if br and cand.get("wave_landed"):
                # -D is safe HERE only: its PR merged per gh, so the content is on the default
                # branch even where a squash leaves ancestry blind (-d would refuse).
                run(["git", "branch", "-D", br], timeout=30)
            elif br:  # -d only: refuses anything unmerged, so this can never lose work
                run(["git", "branch", "-d", br], timeout=30)
        run(["git", "worktree", "prune"], timeout=60)
    out({"mode": "apply" if a.apply else "dry-run",
         "merged_pr_heads": merged_why,
         "total_worktrees": len(entries), "kept": len(keep),
         "candidates": [f"{x['path']} ({x['why']})" for x in candidates],
         "removed": removed, "errors": errors,
         "kept_reasons": [f"{x['path']} ({x['why']})" for x in keep]})


def cmd_tidy(a):
    """What a finished run leaves behind, swept in one call: stand-ins nobody owns (and
    the QA login each seeded), worktrees whose branch landed, and landed branches, local and
    remote. Dry-run unless --apply; each part is its own verb and keeps its own guards (a
    dirty tree and a live wave are never touched). The answer names
    what each part could not remove, so a closeout never reads clean when it is not."""
    import subprocess
    import sys
    from hb.rig.common import HB_ENTRY
    parts = [("stand-ins", ["venue", "sweep", "--older-than-hours", str(a.older_than_hours)]),
             ("worktrees", ["worktree-gc", "--include-untracked"]),
             ("branches", ["branch-gc"])]
    report, left = {}, []
    # a stand-in still serving a landed worktree holds its folder open; it goes first
    going = subprocess.run([sys.executable, HB_ENTRY, "worktree-gc", "--include-untracked"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=1800)
    landed = []
    for line in reversed((going.stdout or "").splitlines()):
        if line.strip().startswith("{"):
            try:
                landed = [str(x).split(" (")[0] for x in json.loads(line).get("candidates") or []]
            except ValueError:
                continue
            break
    if landed:
        parts[0][1].extend(["--landed", *landed])
    for name, argv in parts:
        cmd = [sys.executable, HB_ENTRY, *argv] + (["--apply"] if a.apply else [])
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
        js = {}
        for line in reversed((r.stdout or "").splitlines()):
            if line.strip().startswith("{"):
                try:
                    js = json.loads(line)
                except ValueError:
                    continue
                break
        report[name] = js or {"error": ((r.stdout or "") + (r.stderr or ""))[-300:]}
        if not js or js.get("errors") or r.returncode != 0:
            left.append(f"{name}: {js.get('errors') or r.returncode} not removed" if js else f"{name}: no answer")
    out({"verdict": ("swept" if a.apply else "dry-run") if not left else "left-behind", "apply": bool(a.apply),
         "left_behind": left, **report}, 0 if not left else 1)
