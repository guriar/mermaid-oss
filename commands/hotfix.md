---
description: "The door for one small change with the owner present. Launches the harness runner on a sentence, a picture, or an issue number; the runner builds it and opens a PR against the landing branch. This session launches, watches, relays the record, and carries the owner's answers back. The owner merges."
argument-hint: "<#issue | one sentence | .context/hotfix/<slug>> [--shadow]"
allowed-tools: "Bash, Read, Glob, Grep, ToolSearch, SendUserFile"
---

# /hotfix

You do not build. The runner does, outside this chat, as separate seat processes that
write everything to disk. Your job is three things: launch, relay, answer. The page, the
watch, control, the relay, answers, the standing step and the closeout are
`${CLAUDE_PLUGIN_ROOT}/docs/run-session.md`; read it and follow it.

## Launch

- A picture pasted with the ask: describe what it shows in one plain sentence and fold it
  into the ask. The runner reads sentences, not images.
- A folder another session wrote (`.context/hotfix/<slug>/contract.md`): pass the folder
  path as the ask; the runner reads the items from it.
- Run from the repo root in the foreground; `--detach` starts the run as its own process
  and prints its run id, pid, out file and the page's address at once:

```
python "${CLAUDE_PLUGIN_ROOT}/engine/hb.py" run hotfix "<the ask>" [--shadow] --detach
```

- Tell the owner the run id the launch printed.

To re-enter after a stop: the same line with `--resume <run id>`.
