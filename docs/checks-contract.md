# The checks contract

How the harness reads the product's checks (Q1, A14). The builder writes a check for every
case it builds; the harness (`engine/hb/contract/checks.py`) runs them, reads one line per case and
writes one `check-row/v1` row per case. How a check drives a browser or calls the API is the
product's business.

## The case

A case under a rule in `<wave>.json` under `paths.manifest_dir` names `boundary`: `screen` (asserted on
what a user sees), `wire` (on the request and the response), or `both`. A case that needs a
sibling slice's work carries `after_compose: true` and runs only on the composed branch (A18).
`hb contract check` refuses a case with no boundary. A file where no case names one was
written before checks existed and reads as `contract/v1 cases, no checks`.

## The check

One file per rule, named for the rule id (`R-CB-7.py`, `R-CB-7.spec.ts`), under the folder
`harness-config.json` names in `paths.checks`. The file names each of
its cases by id, as written (`CB-08`) or with `_` for `-` (`CB_08`, a function name), and each
case has two halves:

- `setup`: what the agent did to make the situation, replayable on a fresh stand-in;
- `assert`: the outcome, at the case's boundary.

A case its rule's file does not name reads UNMEASURED `no-check`, by name. The states catalog
is optional reuse for a setup, never a requirement (A14).

## Approval

The reviewer approves a check by listing it under `approved_checks`; the runner pins it at its
hash in `approved.json` in the checks folder (path to sha256, line endings read as LF). A check edited
after approval, or never approved, still runs, and its row reads UNMEASURED `unapproved` with
what the check said (`said`) until it is approved again.

## The command

`checks.command` in `harness-config.json`, split like a shell line, forward slashes only, with
three placeholders: `{ids}` the comma-joined case ids to run, `{venue}` a path to the stand-in's
JSON (its receipt: origin, tenant, credentials), `{out}` a folder for captures. The harness calls it
once per rule, one rule at a time, from the product root. No command: every case with a check
reads UNMEASURED `no-runner`.

## Output

Standard output, one JSON object per case; any other line is progress and is ignored.

    {"case": "CB-08", "ok": true}
    {"case": "CB-09", "ok": false, "line": "expected 2 on the count, read 0"}

`line` is the failing line the builder reads on a fix round. A case the runner printed no line
for reads UNMEASURED `no-result`, or `checks-runner` when the runner exited non-zero.

## The row

`check-row/v1` (`engine/hb/contract/checks.py`): rule, case, boundary, state (WORKS, BROKEN with
the failing line, UNMEASURED with its substate), the check's path and hash, and the identity
tuple (tip, contract hash, harness, venue). Rows land in `<out>/check-rows.jsonl`.
