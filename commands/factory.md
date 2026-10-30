---
description: "The factory: the harness's instruments as steps a session walks on an ask of any size, with no contract. A worktree, the work, a review of the diff against the ask, the gates, an audit on the venue when there is a screen, and the landing; each step one hb verb with a receipt. The owner says when it is done."
argument-hint: "\"<the ask, in a sentence>\""
allowed-tools: "Bash, Read, Write, Edit, Glob, Grep, ToolSearch"
---

# /factory

You are the one who understands and the one who builds; nothing is handed to a fresh seat. The
harness lends you its instruments one at a time, and every instrument answers with a receipt the
record keeps. There is no contract here: the ask is the goal, "done" is the owner's word, and the
record says so in those words.

## The steps

1. **A worktree.** From the repo root: `python "${CLAUDE_PLUGIN_ROOT}/engine/hb.py" worktree open <slug>`.
   The receipt names the worktree, its branch `here/<slug>` and the base sha. Work there, never in
   the owner's checkout. A second open of the same slug returns the same worktree.
2. **The work.** Read, experiment, change, commit as you go. This is the part the harness does not
   script.
3. **Eyes, whenever you want them.** Inside the worktree:
   `python "${CLAUDE_PLUGIN_ROOT}/engine/hb.py" review --here "<the ask>"`. One reviewer seat reads
   the diff from the base to your tip against the ask and the repo's standards, and the receipt
   lists its findings and the ones to fix. Fix them yourself and ask again.
4. **The gates.** `python "${CLAUDE_PLUGIN_ROOT}/engine/hb.py" run-gate -- <the product's gate command>`
   for any gate you want to see; the landing runs every heavy gate at the tip on its own.
5. **An audit, when there is a screen.** `python "${CLAUDE_PLUGIN_ROOT}/engine/hb.py" audit --here`.
   The venue stands up through the product's venue block, one audit seat drives the change the way a
   user would, and the receipt carries its rows. Without `ui` in the venue's capabilities the receipt
   reads `not offered: ui` and nothing sits.
6. **The landing, when the owner says done.** `python "${CLAUDE_PLUGIN_ROOT}/engine/hb.py" land`.
   The record and the PR body come from the same lander the beats use, with the review's findings,
   the audit's rows and the gates at the tip, and the PR opens against the landing branch.
   `--shadow` writes the record and no PR.

## What to tell the owner

- After step 1: the worktree path and the branch, once.
- After a review: the findings in one line each, and what you changed for them.
- After the landing: the PR, the record's finish state, and the line the record carries about the
  contract: none was supplied and completeness was not measured. Say it in those words.

## When the shape is known

If the ask turns out to be one small change with one clear item, `/hotfix` with the sentence or
the worktree's branch is the same road with a contract. If it grows into a surface or a rule, it
is a slice, and the design sitting rules it first. The factory is the door for work whose shape
you do not know yet.
