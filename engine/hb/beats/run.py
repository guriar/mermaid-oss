"""hb.run - the runner: a Python process that sequences the stages of a beat and
spawns every seat as a subprocess.

Agents spawn nothing, watch nothing, message nothing. A seat reads a brief file,
does one job in a worktree, and writes one report the runner validates against
its schema and the tree's head. The stage order lives here, in code, so a step
cannot be forgotten and costs no tokens.

Seams:
  schemas   the report envelope and the per-seat shapes
  journal   the run journal: phases, operation records with idempotency keys
  adapters  one launcher per runtime (claude, codex, fake for drills)
  seat      run one seat and validate what it wrote
  policy    what the runner decides about rows and findings: the stop list, grey, proof by test
  briefs    render a seat's brief from the role file and the facts on disk
  hotfix    the run verb: the hotfix, slice and wave beats
"""
from hb.beats.hotfix import cmd_run, register_run

__all__ = ["cmd_run", "register_run"]
