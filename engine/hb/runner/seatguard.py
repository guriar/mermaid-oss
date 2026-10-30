"""hb.run.seatguard - the PreToolUse hook a Claude seat's shell runs through.

A verifier once slept three hours in a python loop for a timed state to arrive on the wall
clock, while the other shards had finished. A seat never waits on the clock: a state that depends
on time is built relative to the venue's now, so it exists already. The hook reads the tool
call Claude Code hands it on stdin and refuses a command that waits: a sleep inside a loop,
or one sleep of a minute or more. A short single sleep (a server coming up) passes.

A builder may spawn helpers (A17), session-style: foreground only, text back, never a commit,
never a child of their own. The hook refuses an Agent call in the background, an Agent call
from a helper, and a git commit or push from a helper. A helper is known by the subagent id
Claude Code hands the hook, or by `HB_HELPER=1`, which every helper brief puts on its commands.
"""

import json
import re
import sys

LOOP = re.compile(r"\b(while|until|for)\b")
SLEEP = re.compile(r"(?:\btime\.sleep\(|\basyncio\.sleep\(|(?<![\w.])sleep\s+|\bStart-Sleep\b[^\n;|&]*?(?:-s(?:econds)?\s+)?)\s*(\d+(?:\.\d+)?)?", re.I)
CLOCK = re.compile(r"datetime\.now|time\.time\(\)|date\s*\+%s|Get-Date", re.I)
REFUSED = ("A seat never waits on the clock. A state that depends on time is built relative to the venue's "
           "now through its state commands, so it exists already, then driven. A command that sleeps in a "
           "loop, or for a minute or more, is refused; a state no "
           "door can reach without waiting is UNMEASURED, named with the door that was missing.")


def waits_on_the_clock(command):
    """Why the command waits, or None."""
    text = command or ""
    sleeps = [m for m in SLEEP.finditer(text)]
    if not sleeps:
        return None
    if LOOP.search(text) or CLOCK.search(text):
        return "a sleep inside a loop or beside a clock read"
    for m in sleeps:
        secs = m.group(1)
        if secs is None:
            return "a sleep with no number"
        try:
            n = float(secs)
        except ValueError:
            return "a sleep with no number"
        if "Start-Sleep" in m.group(0) and "-m" in m.group(0).lower():
            n = n / 1000.0
        if n >= 60:
            return f"a sleep of {secs} seconds"
    return None


HELPER_TOOLS = ("Agent", "Task")
HELPER_MARK = re.compile(r"\bHB_HELPER=1\b")
COMMITS = re.compile(r"\bgit\b[^\n;|&]*\b(commit|push)\b")
BACKGROUND = ("A helper runs in the foreground and hands its text back before you go on. A background "
              "helper is stopped when your reply ends and its work is lost. Call it again without "
              "run_in_background.")
NO_CHILD = ("A helper never spawns a helper of its own. Do the step yourself and return what you saw "
            "to the builder that called you.")
NO_COMMIT = ("A helper never commits or pushes. Leave the tree as it is and tell the builder what to "
             "commit; the builder owns every commit on its branch.")


def from_a_helper(call, command=""):
    """Whether the call comes from a builder's helper rather than the builder itself."""
    return bool(call.get("agent_id")) or bool(HELPER_MARK.search(command or ""))


def refusal(call):
    """The plain sentence a call is refused with, or None when it may run."""
    tool = call.get("tool_name")
    args = call.get("tool_input") or {}
    if tool in HELPER_TOOLS:
        if from_a_helper(call):
            return NO_CHILD
        if args.get("run_in_background"):
            return BACKGROUND
        return None
    if tool != "Bash":
        return None
    command = args.get("command") or ""
    if from_a_helper(call, command) and COMMITS.search(command):
        return NO_COMMIT
    why = waits_on_the_clock(command)
    return f"{why}. {REFUSED}" if why else None


def main():
    try:
        call = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    why = refusal(call if isinstance(call, dict) else {})
    if not why:
        return 0
    sys.stderr.write(f"Refused: {why}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
