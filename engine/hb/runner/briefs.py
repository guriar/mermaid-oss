"""hb.run.briefs - render a seat's brief from its role file and the facts on disk.

A brief is the role text (one job, one output schema) followed by an `# Inputs` block the
runner fills from files, whole: the slice issue, the contract, the rulings, the venue, the
note, the findings. The runner filters, orders and cuts none of it. The last lines are the
envelope: what a model cannot know from the repo (where it works, what it echoes, where its
progress file and report go, how the venue, the checks and the helpers are reached, and the
runtime's two limits).
"""
import json
from pathlib import Path

from hb.rig.common import HB_ENTRY, canon


def helpers_line(evidence_dir):
    """The product's checks folder and its checks runner, and how a helper the builder spawns is
    known to the seat guard (A17, Q1)."""
    from hb.rig.common import cfg_or_empty
    from hb.contract import checks
    c = cfg_or_empty()
    folder = ((c or {}).get("paths") or {}).get("checks")
    where = f"Checks folder: `{folder}`" if folder else "Checks folder: none named in the product config (`paths.checks`)"
    runner = checks.command(c)
    run = (f"Checks runner: `{' '.join(runner)}` (it fills `{{ids}}`, `{{venue}}` and `{{out}}`)" if runner
           else "Checks runner: none named in the product config (`checks.command`); a case reads UNMEASURED no-runner")
    return (f"{where}. {run}. A helper you spawn starts each shell command with "
            f"`HB_HELPER=1 `, runs in the foreground, and never commits; its evidence goes under "
            f"`{Path(evidence_dir).as_posix()}`.")


def roles_dir():
    """The harness repo's roles/ beside engine/, else the product's .claude/roles/."""
    here = Path(HB_ENTRY).resolve().parent.parent / "roles"
    if here.exists():
        return here
    alt = canon() / ".claude" / "roles"
    return alt


# A seat that ends its reply while a background command runs exits with that command
# stopped and no report (five seats in one slice); the runner raises the foreground
# command limit to an hour, so a long suite runs in the foreground instead.
FOREGROUND_ONLY = ("Run every command in the foreground and wait for it. A command started in the "
                   "background is stopped the moment you finish your reply, and you exit with no "
                   "report. A command may run up to 60 minutes: pass a timeout of 3600000 for a long suite.")

# A verifier slept three hours in a loop for a timed state to arrive on the wall clock while the
# other shards were done; the seat's shell refuses such a command (seatguard).
NO_CLOCK_WAIT = ("Never wait on the clock. A state that depends on time is built relative to the venue's now "
                 "through its state commands, never by waiting on the clock; a command that sleeps in a loop or "
                 "for a minute or more is refused, and a state no door reaches without waiting is UNMEASURED, named.")


def tools_line(role, tools):
    """What a seat that drives the venue is handed: its browser tool, and the venue's `say` line
    when it offers `model`, so the audit and discovery roles type the exact command."""
    from hb.rig.common import cfg_or_empty
    from hb.runner import adapters
    from hb.venue import adapter
    if not adapters.role_tools(role):
        return ""
    tools = [n for n, spec in (tools or {}).items() if spec]
    lines = [f"Browser tool: `{', '.join(sorted(tools))}`" if tools else adapter.not_offered("ui")]
    c = cfg_or_empty()
    if c.get("venue") and adapter.offers("model", c):
        say = " ".join(adapter.argv("say", name="<venue name>", text="<the words>"))
        lines.append(f"The product's model answers through: `{say}`")
    else:
        lines.append(adapter.not_offered("model"))
    return "\n".join(lines)


ROLE_FILES = {"build": "builder", "review": "reviewer", "verify": "audit", "discovery": "discovery"}


def role_text(role):
    """The role's one page. A fix round is the builder relaunched on its own page (hb.run.fixing)."""
    p = roles_dir() / f"{ROLE_FILES.get(role, role)}.md"
    if not p.exists():
        raise FileNotFoundError(f"no role brief at {p}")
    return p.read_text(encoding="utf-8")


def _section(title, body):
    if body is None or (isinstance(body, str) and not body.strip()):
        return ""
    if not isinstance(body, str):
        body = "```json\n" + json.dumps(body, indent=2, ensure_ascii=False) + "\n```"
    return f"\n## {title}\n\n{body.rstrip()}\n"


def render(role, facts, envelope):
    """facts: ordered dict of section title -> str | json-able, rendered whole in the order
    given. envelope: the fields the report must echo (issue, run_id, attempt, base_sha), the
    worktree (`cwd`), the evidence dir, the progress file and OUTPUT_PATH."""
    parts = [role_text(role).rstrip(), "\n\n# Inputs\n"]
    for title, body in facts.items():
        parts.append(_section(title, body))
    env_lines = ["\n## Envelope\n", "Echo these in your report exactly:\n"]
    for k in ("issue", "run_id", "attempt", "base_sha"):
        if k in envelope:
            env_lines.append(f"- {k}: `{envelope[k]}`")
    env_lines.append("- head_sha: the output of `git rev-parse HEAD` in your working directory when you finish")
    env_lines.append(f"\nWorktree (already your cwd): `{envelope['cwd']}`; the base commit is `{envelope.get('base_sha')}`")
    env_lines.append(f"Evidence directory (write scratch and captures here): `{envelope['evidence_dir']}`")
    if envelope.get("progress"):
        env_lines.append(f"Progress file (yours; read it first if it exists, write your plan as its first entry, "
                         f"keep it current): `{envelope['progress']}`")
    if role == "build":
        env_lines.append("\n" + helpers_line(envelope["evidence_dir"]))
    from hb.runner.schemas import stop_kinds
    env_lines.append(f"\nStop kinds: {', '.join(f'`{k}`' for k in stop_kinds())}")
    tools = tools_line(role, envelope.get("tools"))
    if tools:
        env_lines.append("\n" + tools)
    env_lines.append(f"\n{FOREGROUND_ONLY}")
    env_lines.append(f"\n{NO_CLOCK_WAIT}")
    env_lines.append(f"\nOUTPUT_PATH: `{envelope['output_path']}`, in the schema your role page gives "
                     "(write it as UTF-8; a Windows shell's default code page is not)\n")
    parts.extend(l + "\n" for l in env_lines)
    return "".join(parts)
