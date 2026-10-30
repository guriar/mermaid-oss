# <product> agent workflow

<One paragraph: what the product is, who uses it, where it runs. This file is the entry point
for any agent working in this repo; the harness is the mermaid plugin.>

## The doors

1. **Small change, owner present: `/hotfix`.** One ask, one contract item, the smallest diff at
   the cause, gates, a PR against the landing branch. The owner merges.
2. **Several small changes at once: `/sweep`.** One hotfix run per issue, in parallel, a PR each.
3. **An ask of any size, no contract: `/factory`.** The session does the work itself in a
   worktree and walks the instruments one by one: a review of the diff against the ask, the
   gates, an audit on the venue, the landing. The owner says when it is done.
4. **What shipped, on film: `/demo`.** After a merge, the change shown through the product's
   screens the way a user sees it. Never a gate.

Discoveries go to `/docket` as causes; the owner rules on them. <A product's own method may add
doors of its own; name them here with what each is for.>

## How this product is reached

<The venue: the command strings under `venue` in `.claude/harness-config.json`, which
capabilities it declares (accounts, ui, model, states, deploy, database), and where its states
catalog lives. A capability it lacks reads "not offered" in every record; never fake it.>

## Invariants that bite from any directory

<The rules every change obeys, each with its reason, so an agent can extend it to a case this
list did not name. Examples: every mutation is idempotent; a new response field is tolerated
when absent; the default branch and how PRs land; the one lockfile; what never enters the repo.>

## How we write code

<The principles (YAGNI, simple first, delete aggressively, extract after the second
duplication, fail fast, semantic naming) and the rules that are cases of them. A test pins one
behaviour. A state a screen can be in enters the states catalog with its build, undo and check
in the same change. No em dashes in copy.>

## Where things live

<Product doc, architecture, glossary, decision registers under `paths.decisions_dir`, contract
manifests under `paths.manifest_dir`, the delivery ledger at `paths.ledger_file`.>
