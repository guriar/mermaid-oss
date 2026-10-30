# hello-web

The example project the mermaid plugin runs on: one page, one API, the standard library.

```bash
python app/server.py --port 8765        # http://127.0.0.1:8765
python -m unittest -q                    # the gate
python tools/venue.py up --name demo --worktree .   # what the harness runs
python tools/venue.py state build greeting-empty --name demo
python tools/venue.py state check greeting-empty --name demo
python tools/venue.py down --name demo
```

The harness works in git worktrees and lands PRs, so first copy this folder out of the plugin tree, run
`git init`, commit, add a GitHub remote, and set `repo` in `.claude/harness-config.json` to it.

To run a hotfix on it: install the mermaid plugin (the plugin's README), open this folder in
Claude Code, and type `/hotfix "the heading should read Hello there" --shadow`.
