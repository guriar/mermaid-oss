---
description: "The door for several small changes at once, as issue numbers or one issue that lists them. Launches one harness hotfix run per issue, in parallel, each with its own PR against the landing branch. This session launches, watches, relays each record as it lands, and carries the owner's answers back. The owner merges each PR."
argument-hint: "<#issue [#issue ...] | #list-issue>"
allowed-tools: "Bash, Read, Glob, Grep, ToolSearch, SendUserFile"
---

# /sweep

You do not build. The runner does, one process per issue, outside this chat. Your job is
three things: launch, relay, answer. A sweep is nothing but several `/hotfix` runs started
at once; each run is watched, operated, relayed and answered as
`${CLAUDE_PLUGIN_ROOT}/docs/run-session.md` says, one watcher per run id.

## Launch

- Start one run per issue from the repo root in the foreground:

```
python "${CLAUDE_PLUGIN_ROOT}/engine/hb.py" run hotfix "#<N>" --detach
```

- Tell the owner every run id.

## Relay

Relay each run the moment it stands or stops, so the owner can merge while the rest run.
