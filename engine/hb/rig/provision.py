"""hb.provision - a run's worktree made ready: the harness files copied in, the product's own
preparation run."""

import json

from hb.rig.common import now_iso, out, run


def _provision_tree(a, c, root, wt, prepare=None):
    """Every WRITE spawn-prep makes into the target tree, in one place.

    Called only after every check has passed. Provisioning first and refusing second left
    half-prepared trees behind: a dry run refused and still stamped an owner marker and copied
    the overlay, so the next call inherited a tree it did not make. `prepare(wt)` is the product's
    own preparation, `(ok, tail)`, handed in by the beat (rig imports only rig), in practice
    `hb.venue.adapter.prepare_worktree`. Returns the provisioned list."""
    prov = []
    (wt / ".claude").mkdir(parents=True, exist_ok=True)
    # nothing tracked is copied: the worktree is a checkout of its branch, the config is read from the
    # canon checkout, and the commands come from the plugin; a copy of a tracked file was the
    # modification the guard below refuses (it once showed up as line endings alone)
    # Worktree ownership is exclusive in every direction: the owner is stamped here and every
    # landing operation verifies marker and branch. A shared wave worktree names the wave as
    # the durable owner and accumulates the issues that provisioned into it, never overwrites.
    marker = wt / ".claude" / "worktree-owner.json"
    prior = {}
    if marker.exists():
        try:
            prior = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prior = {}
    issues = list(prior.get("issues") or ([prior["issue"]] if prior.get("issue") else []))
    if a.issue not in issues:
        issues.append(a.issue)
    owner = {"wave": a.wave, "issue": a.issue, "issues": issues,
             "shared": len(issues) > 1,
             "first_provisioned": prior.get("first_provisioned") or now_iso(),
             "provisioned": now_iso()}
    marker.write_text(json.dumps(owner, indent=1), encoding="utf-8")
    prov.append(f".claude/worktree-owner.json (owner stamp; issues={issues})")
    # The product's own preparation (linking its packages, say), so a gate in this tree reads
    # this branch's bytes and not the host checkout's. A failure refuses the tree.
    if prepare is None:
        prov.append("prepare_worktree (not asked: the caller handed no preparer)")
    else:
        prepared, tail = prepare(wt)
        if not prepared:
            out({"error": "spawn-prep: the product's venue.prepare_worktree command failed in this "
                          "worktree, so a gate here may measure another tree. Provisioning refuses.",
                 "verdict": "refused-prepare-worktree", "tail": tail,
                 "provisioned": prov, "worktree": str(wt)}, 2)
        prov.append("prepare_worktree")
    # Provisioning must NEVER modify a TRACKED file: a stale copy from a dirty checkout nearly
    # reverted canon lines through a slice's broad `git add`. Everything this writes is
    # untracked; a tracked modification means a bad recipe and the spawn must not proceed.
    rc, o, _ = run(["git", "-C", str(wt), "status", "--porcelain"], timeout=90)
    tracked_mods = [ln for ln in o.splitlines() if ln.strip() and not ln.startswith("??")]
    if tracked_mods:
        out({"error": "provisioning guard: tracked files are MODIFIED in the fresh worktree, "
                      "likely a stale-copy recipe (CLAUDE.md/CONTEXT.md are TRACKED; never copy "
                      "them). Fix with `git checkout -- <file>` in the worktree, find the "
                      "copier, then re-run spawn-prep.",
             "tracked_modifications": tracked_mods[:20], "provisioned": prov}, 2)
    return prov
