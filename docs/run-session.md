# The session beside a run

Every door that launches a run (`/hotfix`, `/sweep`, and the ones a product's method adds)
then does the same things beside it. This is that text, written once; each command says what is its own and points
here for the rest.

## The page

The owner watches on the run page, not in the chat, at one address that never changes:
give the owner http://127.0.0.1:4950/, never a link to one run. If the home does not answer,
run `python "${CLAUDE_PLUGIN_ROOT}/engine/hb.py" page --install` once on this machine (Windows; elsewhere
start `hb page` yourself and keep it running). A stop answered on
the page starts the run again itself; do not also resume it from the chat.

## Watch

Watch the run through one verb, never by reading the folder on a timer. Right after the
launch, and again every time the watcher returns, start it in the background with the cursor
the last return printed (0 the first time):

```
python "${CLAUDE_PLUGIN_ROOT}/engine/hb.py" run watch <run id> --after <cursor>
```

On each return tell the owner ONE line in plain words, after reading the seat's report
under `seats/<role>-<n>/report.json`. A `QUESTION` line is relayed verbatim the moment it
appears. Stop watching when the JSON names a `final` phase, then relay.

## Control

You operate a run through `control.txt` in its run folder, one verb per line. You never steer
the work.

- `pause`: finish the seat that is running, then stop; `--resume <run id>` re-enters.
- `cut`: kill the seat that is running now; the runner relaunches it from its progress file.
- `answer: <id>: <text>`: the owner's answer to one stop, by the id the record prints.
- `answer: <text>`: an answer that names no stop; every seat that starts afterwards reads it.

## Relay

When the watch names `standing`, `stopped` or `paused-limit`, read `evidence/record.md` in the
run folder, the run's one record, and relay it short: the finish state and the PR link, every
line of `Yours to rule` verbatim, the item counts, what is red or unmeasured with the reason
the record gives, and the PR's `Time and cost` lines as written. The owner merges.

## Answer

A `stopped` run names what it needs. Ask the owner in one line, write the ruling as
`answer: <id>: <text>` in `control.txt`, then re-enter the same run with the launch line and
`--resume <run id> --detach`.

## Standing: the next step

A run that opened its PR stands until its merge. The owner's next ask for it goes in one
line, then watch again from the last cursor:

```
python "${CLAUDE_PLUGIN_ROOT}/engine/hb.py" run next <run id> <what the owner asks for>
```

The builder takes the words, the run's loop measures the branch again, and the PR refreshes.
`cut` in `control.txt` ends the run at its verdict.

## After the merge

```
python "${CLAUDE_PLUGIN_ROOT}/engine/hb.py" closeout <run id>
```

Relay its verdict in one line, and every `left behind` and every kept line, one line each.
The closeout starts the demo; when `evidence/demo/launch.json` reads `filmed`, send the owner
the video and `chapters.md` with `SendUserFile`, and the summary.
