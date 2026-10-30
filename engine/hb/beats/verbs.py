"""The one table of hb verbs.

cli.py builds its dispatch from this table, and `hb` refuses to start when the subparsers
it registered and this table disagree. A verb exists here or it does not exist.

Kinds: a DOOR is a verb an agent or the owner types; INTERNAL is reached only from
another verb.
"""

DOORS = {
    "ci-watch": "watch PR checks with backoff and outage classification",
    "deploy-watch": "watch the deploy run to conclusion",
    "gh": "mutex-guarded gh passthrough: serializes credential access across worktrees",
    "docket": "the owner's queue: add, list, verdict, retype, render, draft, dryrun",
    "run-gate": "sanctioned gate runner: direct exec, evidence capture, TRUE_EXIT line",
    "ledger": "the delivery ledger: receipt, append",
    "venue": "a stand-in stack for a run: up, down, list, seats, sweep",
    "venues": "the dev venues the harness made: sweep --expired purges the ones past expiry, --dry-run names them",
    "rig": "the harness pinned into a product: drill a commit, install (pin it)",
    "run": "the runner doors: hotfix, slice, wave; watch prints a run's boundaries",
    "worktree": "a worktree a session works in, off the landing branch: open <slug>",
    "review": "one reviewer seat on the worktree you stand in, against your ask: --here <ask>",
    "audit": "the venue up and one audit seat driving the worktree you stand in: --here",
    "land": "the record and the PR from the worktree's receipts, against the landing branch",
}

INTERNAL = {
    "branch-gc": "sweep landed branches; wave-close calls it",
    "worktree-gc": "sweep merged worktrees; wave-close calls it",
    "tidy": "sweep stray stand-ins, landed worktrees and landed branches in one call",
    "page": "the run page: every run, where each is, and what needs the owner, on this machine",
    "sittings": "back up every sitting record to the private repository in the config",
    "contract": "a rules-with-states contract: check it is signable, or print its filing card",
    "closeout": "after the merge: sweep what a run left, check it is gone and live, name the red rows it carried, start the demo",
    "demo-launch": "after a merge: wait for its deploy, then film it with the demo seat; the closeout starts it",
}


def live():
    return sorted(set(DOORS) | set(INTERNAL))


def check_registered(subparsers):
    """Refuse to start when the parser and the table disagree."""
    registered = set(subparsers.choices)
    missing = registered - set(live())
    unregistered = set(live()) - registered
    if missing or unregistered:
        raise RuntimeError(f"hb verb table and parser disagree: not in verbs.py {sorted(missing)}, "
                           f"no subparser {sorted(unregistered)}")


