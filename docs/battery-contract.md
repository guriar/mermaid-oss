# The battery contract

How the harness reads the product's battery. The product builds the runner to
this shape; the harness (`engine/hb/contract/battery.py`) runs it, grades it and writes the row.
Nothing else about the battery is the harness's business: where the golden file lives, how a
case is graded, what k costs, are the product's.

## The item

A contract item `held_by: model` names `battery: <case set id>`. Optional: `pass_rate:
"N/M"` is its bar (the share of cases that must pass all k tries; default 4/5), and `k:
<tries per case>` when the item wants one. A model item without `battery:` reads UNMEASURED
"no battery" by name; no tester grades it, and the tester's discovery brief covers it.

## The command

`harness-config.json` names the runner under `battery.command`, a string split like a shell
line, forward slashes only (`python tools/battery.py run`). With `battery.scope: new`
the battery runs only the golden cases the golden file holds at the tip and not at the base,
handed to the runner as `--ids`, once, at the audit, at `battery.k` (default 1), never in the
loop's rounds and never the whole file; a set the change added nothing to reads UNMEASURED
"adds no golden case" by name. Without a scope the loop runs each model item's set at
`battery.k` and the audit at k=3. The dollar ceiling `battery.ceiling_usd` bounds both (Q4).
No `battery.command` means the product has no battery runner: every model item reads
UNMEASURED "no battery" and falls to the tester for discovery.

The harness appends: `--set <case set id> --out <dir>` always; `--ids <a,b>` under scope new;
`--backend-url <url>`,
`--env-file <path>` (the stand-in's credentials) and `--tenant-id <id>` when the venue knows
them; `--k <n>` when the item or the config names one. It runs from the worktree, under the
tester seat's time ceiling and its own admission grant, once per item per drive, before any
tester sits; past the ceiling its whole process tree is killed.

## Output

Standard output, one JSON object per line; any other line is ignored (progress is fine).

One line per case try:

    {"case": "add-03", "say": "add two of the blue ones", "try": 1,
     "call_ok": true, "card_ok": true, "state_ok": true, "reply_judge": "ok", "ms": 1830}

`case` is the golden id, stable across runs. `say` (optional) is the user's sentence; the
harness lists it in the tester's discovery brief. `call_ok`, `card_ok`, `state_ok` are the
code grader's three booleans: the tool call, the card, the venue state after OK. `reply_judge`
is the judge on the reply's words only: `"ok"`, `"off"`, or `null` when nothing was judged.

One summary line, last:

    {"cases": 15, "k": 3, "pass_k": 0.93, "ask_rate": 0.07, "extra_calls": 0}

`ask_rate` is the share of tries that asked instead of acting; `extra_calls` the tool calls
beyond the expected one. Both ride the record; neither grades the item. Optional `usd` is what
the set's run spent on the model, in dollars: the harness sums it over a run and, once the sum
passes `battery.ceiling_usd`, every set left reads UNMEASURED "not run: ceiling". A runner that
prints no `usd` is journaled `battery-spend` unmeasured and never stopped by the ceiling.

The harness checks the output against this shape before it grades: at least one case,
`cases` equal to the number of cases printed, exactly k tries for every case (the k it asked
for, else the summary's), no try marked `"rate_limited": true`, and every case the set held on
its last record on this branch's lineage. Anything else is "output malformed", named, and
read the same as `3`. A runner that drops an errored case would otherwise raise pass^k.

## How the harness grades

A try passes when `call_ok`, `card_ok` and `state_ok` are all true and `reply_judge` is not
`"off"`. A case passes when all k tries did. pass^k is the share of cases that passed. The
harness counts this from the case lines; the summary's `pass_k` is kept beside as the
runner's own word. WORKS when pass^k is at or over the bar, BROKEN/failed naming the failing
cases otherwise. A tester find on a WORKS row makes it BROKEN naming the find, until the
golden set holds the case. The tester seat never erases the battery's row: a seat that
crashes or leaves the item out keeps it.

## Exit codes

- `0` the set ran; grade from the lines.
- `4` the runner does not know the case set, with a last JSON line `{"unknown_set": "<id>"}`:
  UNMEASURED "no battery", the tester drives discovery, the PR names it. Never a silent pass.
  (`2` is argparse's usage error: a flag the runner refused, read as a crash.)
- `3` the environment: a rate-limit storm, the venue unreachable, no model. UNMEASURED with the
  signature `battery-runner`, rerun once on the next round, never handed to a fix seat.
- Anything else, a timeout, or output with no summary line: a crash, read the same as `3`.

A try the runner itself retried after a 429 is one try. A try it could not measure for a
429 is printed with `"rate_limited": true`, never as a failed try.

## Proposed cases

`battery.golden` in `harness-config.json` names the product's golden file. After a slice's
build, the discovery seat tries to break every model item's act on the real model; each find
(a user's sentence and what went wrong) on an item with a case set is appended to the
file's top-level `proposed` object, keyed by set id, in the frozen cases' own shape with
`"status": "proposed"`, the sentence as its one `say` step, what went wrong as its `note`,
and an empty `expect`. The frozen `cases` list is never touched: the harness rewrites the file
only when its own JSON writer reproduces the file byte for byte, else it proposes nothing and
says why. The builder commits the proposed cases with its fix. The reviewer accepts a
proposed case against the rulings, writes its `expect` and moves it into `cases`; the PR lists
every one for the owner's veto. The runner skips `proposed`, so a fresh find never grades
the set until it is accepted. Each model item's row carries `proposed`, the count its finds
added.

## Where things land

Under the run's evidence: `battery/<item>/runner.out` and `runner.err`, cited by the row.
`battery-scores.json` in the record: per case set, pass^k, k, the case count and ids, the
failing ids, the bar, the code tip, the base and the run id. The next run compares only with
the newest record of the same set, the same k and the same case ids whose tip is an ancestor
of its base (this branch's lineage, never a sibling's), and prints pass^k against it. The journal holds one
`battery-run` event per run, one `tester-find` per item and one `discovery-finds` per
slice with the count found and proposed; the reducer folds the first two.
