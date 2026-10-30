# Mermaid

Mermaid is a Claude Code plugin that takes a change request in one sentence, has one Claude
session build it in a worktree and another review it, runs your tests, drives the change on a
running copy of your app when you give it one, and opens a pull request that says what works, what
is broken and what was never measured, in those words. Your project describes itself in one config
file, `.claude/harness-config.json`: its test commands, its branches, and the optional script that
starts your app. The engine carries no fact about any product.

This public cut ships `/hotfix` (one small change), `/sweep` (several at once), `/factory` (work whose shape
you do not know yet), `/demo` (what shipped, on film) and `/docket` (what agents raised). The rest of the method (the
design sitting, the slice pipeline, the wave, the promotion ceremony) is described below so the
shape is clear, and is not in this repository. The full method runs daily on a private product;
this repository is the engine and one complete lane of it, cut so the engine can be read and run
on its own.

## What a hotfix run does

```
/hotfix "the heading reads Hello twice"
   │
   ├─ worktree       a branch off the landing branch, in its own folder
   ├─ venue          the product brought up through its own commands (optional: a venue is declared, never assumed)
   ├─ build          one model seat, the whole ask, the repo and the tools; it plans its own work
   ├─ gates          the product's own checks, each quoted as its TRUE_EXIT line
   ├─ review         a second seat reads the diff against the ask; reds go back to the builder
   ├─ audit          a blind seat drives the change the way a user would, when the venue offers a screen
   └─ PR             the record: what works, what is broken, what was never measured, in those words
```

Four rules hold the whole thing up:

- **A green is a measurement, never a claim.** A gate's result is the artifact's `TRUE_EXIT=`
  line. A stage whose capability the product does not offer reads `not offered`. Nothing is
  defaulted to pass.
- **The engine knows nothing about the product.** Everything product-shaped is a command string
  or a value in `.claude/harness-config.json` (`docs/config.md`). A missing key is a refusal
  naming the key.
- **A run keeps the engine it started on.** The run freezes its own code at launch, so a plugin
  update under a run in flight changes nothing.
- **The loop stops on facts, not on a reviewer running out of words.** Merge readiness is five
  facts computed from receipts: the goal met with nothing P1 open, every gate green at the tip,
  every measured row green, nothing waiting on the owner, the record published. A fix round opens
  only for a false fact; a finding below P1 after the goal is met rides the record for the owner.
  The heavy gates run once when the loop is quiet and again only at a moved PR tip.

## The method, in one paragraph each

**The design sitting** turns a braindump into a signed contract: a problem statement, a pattern
recon of what the code already does, a grill, screens designed on the live product and frozen as
crops, and a contract file of rules with cases, states and proofs (eyes, model, test) that a
machine checks before anything is filed. **A slice** is one claim in one territory, built
unattended against that contract: the builder reads the issue whole, every item ends as WORKS,
BROKEN or UNMEASURED, and a blind audit drives every case. **A wave** builds independent slices
in parallel, merges each onto a wave branch as it ends, and measures the composed branch once.
**Promotion** is owner-only and every automated lane to production is hook-walled. **The
docket** is where agents file what they found as causes, with a desired state and a lane, and
the owner rules. **The factory** is the harness's instruments as steps a session walks on an ask
of any size with no contract: a worktree (`hb worktree open`), the work, a review of the diff
against the ask (`hb review --here`), the gates, an audit on the venue when there is a screen
(`hb audit --here`), and the landing (`hb land`), each a verb with a receipt, the owner's word as
"done". The beats call the same verbs, so a review is one thing however it was asked for.

## Runtimes

A seat is a row in the product's config: which runtime, which model, which effort. A runtime is
four facts under `runtimes.<name>`: the command template (`{model}`, `{brief}`, `{effort}` and
`{run_dir}` are filled at launch), how the brief is handed over (`stdin` or a path), how a
browser is attached (`mcp` for a CLI that takes an MCP server list, else `none`), and how its
usage line is read (`claude-json`, `codex-text`, or `none`, which records no usage rather than
estimating one). The engine ships `claude`, `codex` and `fake`; a fourth is one JSON block, no
engine edit. A seat that drives the product (the audit) needs a runtime with a browser door.

The slash commands are one door into the engine, for Claude Code. `python engine/hb.py` from any
shell is the other, and takes the same verbs.

## Requirements

Python 3.11 or newer as `python` on your PATH, git, the GitHub CLI `gh` signed in, and Claude Code.
The engine is the standard library only; there is nothing to pip install. Driving the app in a
browser (the audit and the demo) needs Node with `npx` so Playwright can run. The seats run Claude
Opus by default and cost tokens; the seat table in the config picks the model per role. The session
page's installer is Windows-only; elsewhere you start `hb page` yourself.

## Install

```bash
claude plugin marketplace add guriar/mermaid-oss
claude plugin install mermaid@mermaid-oss
```

Then, in a product with a `.claude/harness-config.json` (start from
`templates/harness-config.template.json`), type `/hotfix "<the ask>" --shadow`. Shadow runs every
stage and pushes nothing. The example under `examples/hello-web` is a product you can run it on
in five minutes: one page, one API, the standard library, a venue script that declares `ui` and
`states`.

## Layout

```
commands/     the doors as slash commands (hotfix, sweep, docket, demo, factory)
engine/hb/    the engine, seven foundations: beats, runner, contract, venue, gates, record, rig
roles/        one page per seat: builder, reviewer, discovery, audit
templates/    what a product starts from: AGENTS.md, harness-config.json, the hotfix issue
drills/       the engine's own tests, one folder per foundation; python engine/hb.py rig drill --here
docs/         config.md (every key), VENUE.md, the checks and battery contracts
examples/     hello-web
```

`python engine/hb.py rig structure` checks the seven folders and the one-way imports.
`python engine/hb.py rig words` refuses a product's word in the engine, read from the product's
`product/words.json`. Both run in CI on every
push, with every drill.

## License

MIT.
