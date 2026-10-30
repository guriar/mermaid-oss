# The project config

Everything mermaid knows about a product it reads from the product's `.claude/harness-config.json`.
The engine carries no product fact and no default for one: a key it needs and does not find is a
refusal naming the key. The only defaults are the engine's own (ports, poll seconds, timeouts),
listed at the end.

## The venue

A venue is the product running somewhere the harness can reach: a sandbox, a dev stack, a local
server. The engine never knows how it is built. It runs the command strings the product writes
here, fills the placeholders, and reads one JSON receipt. Every key is a shell line.

```json
"venue": {
  "capabilities": "python tools/venue.py capabilities",
  "up":           "python tools/venue.py up --name {name} --worktree {worktree}",
  "down":         "python tools/venue.py down --name {name}",
  "list":         "python tools/venue.py list",
  "state_build":  "python tools/venue.py state build {state} --name {name}",
  "state_check":  "python tools/venue.py state check {state} --name {name}",
  "state_undo":   "python tools/venue.py state undo {state} --name {name}",
  "states":       "tools/venue-states.json",
  "sign_in":      "python tools/venue.py sign-in --name {name}",
  "ui": {"driver": {"command": "npx", "args": ["@playwright/mcp@0.0.82", "--storage-state", "{session}"]}}
}
```

| key | capability | placeholders | what the engine reads back |
|---|---|---|---|
| `capabilities` | always | | one JSON line: a list drawn from `accounts`, `ui`, `model`, `states`, `deploy`, `database` |
| `up` | always | `{name}` `{worktree}` `{attempt}` `{options}` | the receipt (below) as the last JSON line of stdout |
| `down` | always | `{name}` `{keep}` | exit code |
| `list` | always | | one JSON line: a list of `{name, origin, started}` |
| `reuse` | states | | not a command: a map of path prefixes to `keep`, `reload` or `rebuild`, read against the changed paths |
| `reload` | states | `{name}` | exit code |
| `state_build`, `state_check`, `state_undo` | states | `{state}` `{name}` | exit code; `state_check` 0 means the state holds |
| `states` | states | | not a command: the path of the states catalog, one entry per state with `id`, `says`, `steps`, `undo`, `check` |
| `sign_in` | accounts | `{name}` | one JSON line: `{session: path or token, user: label}` |
| `set_model` | model | `{name}` `{real}` | exit code; `{real}` is `true` or `false` |
| `say` | model | `{name}` `{text}` | one JSON line: the model's reply and the cards it drew |
| `demo_provision` | accounts | `{slug}` | the receipt |
| `database_lease`, `database_release` | database | `{slug}` | one JSON line: `{url}` for the lease |
| `memory` | any | `{name}` | one JSON line: `{bytes}`; absent, the venue is not priced at admission |
| `purge` | any | `{name}` | exit code |
| `prepare_worktree` | any | `{worktree}` | exit code |
| `ui.driver` | ui | `{session}` `{origin}` in `args` | not a command: the tool spec a tester seat gets; nothing else browser-shaped is handed to a seat |

Every placeholder is filled by the stage that calls, and a stage with no value for one refuses;
only `{options}` and `{keep}` may be empty. A key a declared capability needs and the config
lacks is a refusal before any seat starts. A
key for a capability the venue does not declare is ignored and named in the record.

### The receipt

What `up` prints as its last line:

```json
{"origin": "http://127.0.0.1:5173", "served_sha": "e39accb7", "tenant_id": "acme", "facts": {"port": 5173}}
```

`origin` is required. `served_sha` is the code the venue serves, when the venue knows it; the
ledger records it and never guesses it. `tenant_id` is required under `accounts` and the
`no-tenant-identity` refusal fires without it. `facts` is passed to every brief unchanged.

### Not offered

A stage whose capability the venue does not declare writes `not offered: <capability>` in the
record and the ledger row, and runs nothing: the drive and pictures without `ui`, the battery
without `model`, state building without `states`, deploy-watch and the wave's deploy assertion
without `deploy`, the test database lease without `database`. The minimum beat needs no venue at
all: build, gates, checks, review, fix, audit, PR body.

## The gates

```json
"gates": {
  "commands": [
    {"name": "unit", "cmd": "python -m pytest tests -q -n auto", "at": "every-tip", "fans_out": true},
    {"name": "types", "cmd": "npx tsc --noEmit", "isolate": "npx vitest run {id}"}
  ],
  "silent_ok_tools": ["tsc", "eslint"],
  "preflight":  "python tools/gate_preflight.py",
  "signatures": [{"pattern": "database system is starting up", "cause": "database-not-ready"}],
  "merge_hazards": "python tools/merge_hazards.py --worktree {worktree}",
  "remote": {"workflow": "gates.yml", "gates": ["unit"], "timeout_min": 40}
}
```

`preflight` is given the gate's argv and environment on stdin as JSON and prints adjusted argv
and environment, or `{"refuse": "<why>"}`. `signatures` classify a red line by cause.
`merge_hazards` runs on a composed worktree and prints hazards, one per line; any output refuses
the compose. `at: every-tip` names a gate that runs at every tip that reaches the PR. `fans_out`
marks a gate admission may size (its worker count comes from the free memory above the line);
`isolate` is the command that reruns one failing id alone, `{id}` filled. `silent_ok_tools` are
the tools whose empty output on exit 0 is a pass and not an unmeasured gate.
`review_gate.size_hold.exclude_globs` lists the paths the size tripwire ignores. Per gate:
`database: true` asks the venue for a database lease (`venue.database_lease`, `{slug}`) and puts
its url in `DATABASE_URL` for that gate; `fans_out` fills `{workers}` in the command with the
count admission grants; `isolate` reruns one red id alone, `{id}` filled; `outcome` is the regex
that reads one test's result line. The gate's name reaches the preflight as `HB_GATE`. A
preflight may answer `notes`, lines written into the gate's artifact. A signature may carry
`recovery`: `room` (rerun once when memory frees), `lease` (rerun on a fresh lease) or a free word.

## Stops, branches, paths

```json
"stops":    {"extra": ["wages", "tenant"]},
"branches": {"landing": "dev", "promotion_base": "main", "integration_prefix": "wave/", "studio_prefix": "studio/", "deletable_prefixes": ["slice/", "fix/"]},
"paths":    {"manifest_dir": "docs/manifests", "ledger_file": "docs/ledger/delivery.jsonl", "decisions_dir": "docs/decisions",
             "design_dir": "design", "checks": "checks/", "release_doc": "docs/release.md", "slicing_doc": "docs/slicing.md",
             "studio_doc": "docs/studio.md", "hotfix_dir": ".context/hotfix", "worktrees_dir": ".claude/worktrees",
             "sittings_dir": ".context/sittings", "changelog_file": "CHANGELOG.md"},
"sittings": {"backup_repo": "<owner>/<name>, used only when sittings_dir is not its own git checkout"},
"queue":    {"labels": {"wave": "wave:", "autonomy": "autonomy:"}},
"landing":  {"branch_prefix": "hotfix/", "status_context": "Walk", "status_via": "status"},
"pictures": {"viewports": [{"name": "desktop", "width": 1280, "height": 800}, {"name": "phone", "width": 375, "height": 812}]},
"deploy":   {"version_url": "https://api.example.com/version", "workflow": "Deploy"},
"ci":       {"gate_check": "CI Gate", "script_prefixes": ["tools/", "npm run "]},
"rig":      {"source_branch": "main", "source_dir": "../mermaid"}
```

The five stop kinds every builder has are `money`, `migration`, `dependency`, `contradiction`,
`auth`; `stops.extra` adds a product's own. `landing.status_via` is `status` (the engine posts the
Walk status itself) or `dispatch` (it sends a `walk` repository dispatch with sha, state, context, description
and target_url, for a product whose required check is pinned to the Actions app; the product's workflow posts). No `paths` key has a default, and neither has
`rig.source_branch`, the branch `rig drill` drills when run without `--here`.

## The engine's own defaults

`page.port` 4950 · `standing.control_poll_s` 20 · `standing.pr_poll_s` 300 · `launch.hold_s` 0 ·
seat stall windows per role (minutes) in `hb.runner.adapters.STALL_MIN` · gate timeout 90 min ·
admission settle 30 s without growth or 180 s, an empty host forces a heavy start at 90 s, `machine.light_kinds` (checks) admitted at once. Everything else is the product's.
