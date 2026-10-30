#!/usr/bin/env bash
# codex-exec.sh - run codex ONLY from a checkout that is standing on the tip the
# review is supposed to judge.
#
# WHY THIS EXISTS. A codex review run was once launched from the CANON
# checkout instead of the slice worktree. codex read canon's HEAD, found it was
# not the tip it had been told to review, and refused on a HEAD mismatch, about
# twenty minutes into the run. The cwd was wrong for the same reason every other
# other symptom of that family was wrong: canon rests on the landing branch, and nothing asserted where the
# caller stood.
#
# WHAT IT DOES, AND NOTHING ELSE. Assert `git rev-parse HEAD` in the CURRENT
# directory equals --tip, then exec codex with the remaining arguments. It is a
# guard, not a wrapper with opinions: it adds no flags, no prompt, no model.
#
#   tools/harness/codex-exec.sh --tip <sha> exec "review this diff" ...
#
# A refusal names BOTH shas and the cwd, because "codex refused" and "codex was
# pointed at the wrong tree" are different facts that route differently.
# Exit codes: 2 = refused by this guard; anything else is codex's own.
set -u

TIP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tip)
      [ $# -ge 2 ] || { echo "codex-exec: --tip needs a value"; exit 2; }
      TIP="$2"; shift 2 ;;
    --tip=*) TIP="${1#--tip=}"; shift ;;
    *) break ;;
  esac
done

if [ -z "$TIP" ]; then
  echo "codex-exec: REFUSED, no --tip <sha> given. A review with no declared tip"
  echo "cannot be checked against the tree it runs in, and unmeasured is not a pass."
  exit 2
fi

CWD="$(pwd)"
if ! HEAD_SHA="$(git rev-parse HEAD 2>/dev/null)"; then
  echo "codex-exec: REFUSED, cwd is not inside a git worktree."
  echo "  cwd: $CWD"
  exit 2
fi

# Compare on the shorter of the two, so an abbreviated --tip is legal.
N=${#TIP}
if [ "$N" -lt 7 ]; then
  echo "codex-exec: REFUSED, --tip '$TIP' is shorter than 7 characters; a prefix"
  echo "that short can match a commit nobody meant."
  exit 2
fi
if [ "${HEAD_SHA:0:$N}" != "$TIP" ]; then
  echo "codex-exec: REFUSED, HEAD is not the reviewed tip."
  echo "  cwd:  $CWD"
  echo "  HEAD: $HEAD_SHA"
  echo "  tip:  $TIP"
  echo "Stand in the worktree that carries the tip (canon rests on the landing branch)."
  exit 2
fi

echo "codex-exec: cwd=$CWD HEAD=$HEAD_SHA matches tip=$TIP"
exec codex "$@"
