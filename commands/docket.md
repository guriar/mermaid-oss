---
description: "The docket walk. Use when the owner wants to rule on what agents raised, or when a session must file a discovery. Every item is born with a measured CAUSE, a DESIRED state, a cause CLASS, and a LANE (hotfix, slice, sitting, ruling, rig). The walk reads by lane, the owner rules, and approved items file as issues in the lane's template: one hotfix issue per cause, seven-section slice bodies, sitting topics. RULE and CALL stay owner-only."
argument-hint: "[wave slug; omit for the global queue]"
allowed-tools: "Bash, Edit, Write, Read, Glob, Grep, Skill, Agent, TaskCreate, TaskUpdate, TaskOutput, ToolSearch, PushNotification"
---

# /docket

`HB='python "${CLAUDE_PLUGIN_ROOT}/engine/hb.py"'`. The ledger is `.context/harness/docket.json`. Items
exit only by owner verdict. This is a present-owner ceremony; never auto-rule.

## What an item is (docket/v3)

A docket item is a **cause**, not a symptom. Each carries:

- `--plain` what the owner would notice, cost of doing nothing, the recommendation, in
  product language;
- `--cause` the measured why: a file:line, a probe result, a driven behavior. "This looks
  wrong" is a symptom; "`report.ts:142` reads the newest row when the period has none"
  is a cause;
- `--desired` the state that should exist, citing the ruling, register line, manifest item,
  or design record it must satisfy;
- `--cause-class` one of `defect` (this line is wrong), `missing-invariant` (nothing forbade
  it), `wrong-seam` (the logic lives in the wrong layer), `environment` (host or tooling);
- `--lane` one of `hotfix`, `slice`, `sitting`, `ruling`, `rig`.
- `--needs-ruling` on a work item that also waits on the owner's word (a REVIEW cannot
  take the ruling lane; this flag is how the walk sees it).
- `--link <id>` for a different cause at a file:line an open item already names; the same
  cause is one item, its new measurement added with `retype --recurrence`.
- `--recurrence` optional but expected on any defect: the grep and its sibling hits, or
  `none: <what was swept>`.

Lane test, in order: **rig** when the cause is the harness or its tooling, never the
product. **hotfix** when one territory, no migration, no new dependency, no
request field added or removed, no auth or guarded path, no mock needed. **slice** when the
shape is known but a hotfix gate fails. **sitting** when taste or a new surface is involved.
An agent that shapes a taste question has quietly ruled it, so when in doubt between slice
and sitting, sitting.

Symptoms that share a cause are ONE item. Symptoms that do not are separate items even in
the same territory; grouping by territory is how causes got buried.

```
$HB docket add --type RULE|REVIEW|CALL|LIVE|PROPOSAL|BATCH --title "..." --plain "..." \
   --cause "..." --desired "..." --cause-class defect --lane hotfix \
   [--recurrence "..."] [--rec "..."] [--body "<evidence>"] [--wave <slug>] [--discovered-by "#N"]
```

## The walk

0. **Consent lane.** PROPOSAL, BATCH, REVIEW, and LIVE items whose recommendation has no
   threshold, no product trade-off, and no competing option execute now: `verdict approved
   --note "consent-by-default; veto window to <date+3d>"`, one digest line each. RULE and
   CALL never consent by default.
1. `$HB docket list --status open [--wave <slug>]` and read the entries. After a wave closes,
   the rig lane is walked before the next wave launches, because those items make the next
   wave cheaper. Present each lane's count, then walk items. Lead with `plain`,
   then cause and desired in one breath each, then the recommendation. Technical title and
   evidence stay one drill-down away. Batch obvious runs ("discard all six?") but never a
   RULE or PROPOSAL. Items under **UNLANED** predate v3: backfill before presenting
   (`$HB docket retype --id dk-N --lane .. --cause .. --desired .. --cause-class ..`) or
   present them as "no cause measured" and recommend discard.
2. Record every verdict at once: `$HB docket verdict --id dk-N --verdict
   approved|parked|discarded|noted [--note] [--revisit "<condition>"]`. Parked requires a
   revisit condition.
3. **Draft, then file, by lane.** For each approved work item fill the lane's template from
   the item into `.context/harness/docket-actions/dk-N-<lane>.md`:
   - **hotfix** → `templates/hotfix-issue.md`: symptom, where, reproduce, cause, desired,
     recurrence, fence with the eligibility checklist pre-answered, done-when assertions.
     Fill the placeholders the docket does not carry (route, viewport, steps, files, the
     assertions). File one issue per item, labels `hotfix` + territory. The owner points
     `/hotfix #N` at it.
   - **slice** → `templates/slice-issue.md`, the seven-section body of the slicing doc at
     `paths.slicing_doc`. A slice needs rules in the contract; run the shaping pass
     first (an opus agent at medium drafts the proto-contract with rule ids, cases, states,
     and the cut), the owner skims by exception, then
     file it in that shape (through `/to-tickets` where the product has it) with the slicing
     doctrine (the doc at `paths.slicing_doc`):
     independence boundaries, no intra-wave edges, topic-sized, bundle by territory.
     Anything needing taste flags `needs-sitting` and moves to the sitting lane.
   - **sitting** → `templates/sitting-topic.md`: the problem in product language, what was
     measured, what the record already says with the register attached, open questions,
     size guess. File with label `sitting`; a design sitting opens on it.
   - **rig** items are never filed. An approved rig item is fixed in the session in the
     harness's own repo, batched on the next rig train, and installed as the plugin after any
     running wave closes; the verdict note names the commit.
   - **ruling** items are ruled in the walk and never filed; a ruling that produces work is
     re-added as a new item in a work lane with its cause block.
   File through `$HB gh -- issue create --body-file <draft> --label <lane> ...`, only after
   the verdict, never before. Wave-bound bundles carry `wave:<slug>-followups`; follow-up
   waves land cold (no `wave:` or `autonomy:ready`), except P0 and security.
4. Close: `$HB docket list --status open`; read back what remains. One line: ruled N, filed N
   by lane, parked N, remaining N.

## Discipline

- The recommendation is yours; the verdict is never yours.
- An item with no cause is not presented; it is backfilled or discarded.
- Never re-present a discarded item; supersede it if evidence changed.
- `hb docket render` shows open counts by lane and age; an item past its revisit condition
  surfaces there.
