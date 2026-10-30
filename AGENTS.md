# Working on mermaid

Mermaid is the engine that runs product development in the repos it is installed into. Every
change here changes how those repos are built, so the bar is: a command states a rule once, the
engine enforces it, and the reason is written where the rule lives.

## The shape

- Seven foundations under `engine/hb/`: beats (the doors a person opens), runner (seats),
  contract (rules, cases, states, proofs), venue (the product's running sandbox, reached only
  through its command strings), gates (the product's checks and their verdicts), record (what a
  run writes and shows), rig (helpers, versions, drills). Imports flow one way: beats may import
  anything, the middle five may import each other and rig, rig imports only rig.
  `python engine/hb.py rig structure` refuses anything else.
- The engine carries no product fact. A path, a tool, a branch name, a word for the product's
  user: every one is a key in the product's `harness-config.json` (`docs/config.md`), and a
  missing key is a refusal naming it, never a default. `python engine/hb.py rig words` refuses
  a product's word in the tree.
- A product plugs in through the `venue` block: command strings with placeholders, a
  `capabilities` command answering a subset of accounts, ui, model, states, deploy, database, and
  one JSON receipt from `up`. A stage whose capability is absent writes `not offered` and runs
  nothing. `examples/hello-web/tools/venue.py` is the smallest complete adapter.

## How to change it

1. Find the cause before the fix. A failure is filed as what was measured and which rule should
   have caught it, never as a symptom.
2. Change the engine before the prose. If the engine can refuse it, the engine refuses it.
3. Every verb a command names exists in the engine; a mechanical diff of the two is part of any
   command edit.
4. One drill pins one defect, under `drills/<foundation>/`, and prints `TRUE_EXIT=0` only when it
   measured something. `TRUE_EXIT=0 UNMEASURED` and `SKIPPED <why>` are their own states, and a
   missing line is red. `python engine/hb.py rig drill --here` runs them all; CI does too.
5. Gate results are quoted as the artifact's `TRUE_EXIT=` line, never a summary of one.
6. Delete aggressively, extract after the second duplication, fail fast, name things in full
   words. Docs carry current truth: no dates, no issue numbers. No em dashes. LF line endings.
7. Every number of seconds is a fuse or a delay. A fuse bounds a hang and costs nothing on a
   healthy run. A delay fires on every run and stands in for a signal the host did not give;
   it is written beside the signal it replaces, is config, and the run's grade line shows what
   it cost. A delay nobody can name the signal for is deleted.
8. The loop's stopping rule is five facts from receipts (`firstscreen.score`), never a seat's
   estimate; what a finding costs is decided once (`policy.route_findings`). A new rule about
   when work is done goes there, not into a beat.
