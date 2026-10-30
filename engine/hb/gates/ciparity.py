"""hb.run.ciparity - the CI checks the runner runs locally, read from the product's own workflows.

One definition of green: every step of a pull-request workflow that invokes one of the
product's own scripts is either run here the way CI runs it, or named UNMEASURED with the
reason. Which lines invoke a script is the product's to say in config `ci.script_prefixes`: a
prefix ending in `/` is a script path an interpreter may run (`tools/`), any other is a command
head followed by the script's name (`npm run`). No prefix configured, no step is read. The rig
keeps no list of its own; a gate the product adds to CI is a gate the runner runs.

A step runs here when it has no `if:`, every `${{ }}` it reads is a context this module can
answer for a pull request (the event name, the base branch), and every shell variable it reads
is set by its own `env:`. A step of plain script lines runs one gate per line; a step with
shell logic runs whole under bash. Env files an earlier step of the job loads into
`$GITHUB_ENV` are loaded the same way.
"""
import re
import shlex
from pathlib import Path

from hb.rig.yamlmini import _yaml_load

PLAIN_WORD = r"[^\s$|&;<>()`]+"
EXPRESSION = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")
SHELL_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
ENV_FILE_LOAD = re.compile(r"([\w./-]+\.env)\b[^\n]*>>\s*\"?\$GITHUB_ENV")


def prefixes(c):
    """The product's script prefixes, config `ci.script_prefixes`; empty when absent."""
    return [str(p) for p in ((c or {}).get("ci") or {}).get("script_prefixes") or []]


def _prefix_pattern(script_prefixes):
    """One alternation of the prefixes: a path prefix runs straight into the script's name,
    a command head is followed by whitespace and then the name."""
    return "|".join(re.escape(p) if p.endswith("/") else re.escape(p.strip()) + r"\s+" for p in script_prefixes)


def _invokes(script_prefixes):
    return re.compile(r"(?m)^\s*(?:(?:python3?|node|bash|sh)\s+)?(?:" + _prefix_pattern(script_prefixes) + r")\S+")


def _plain_line(script_prefixes):
    return re.compile(r"^(?:(?:python3?|node)\s+)?(?:" + _prefix_pattern(script_prefixes) + r")" + PLAIN_WORD
                      + r"(?:\s+[^\s$|&;<>()`\\]+)*$")


def _contexts(default_branch):
    # a base branch the config does not name is a context only CI's runner can answer
    known = {"github.event_name": "pull_request"}
    return dict(known, **{"github.base_ref": default_branch}) if default_branch else known


def _on_pull_request(doc):
    on = doc.get("on", doc.get(True)) if isinstance(doc, dict) else None
    if isinstance(on, str):
        return on == "pull_request"
    if isinstance(on, list):
        return "pull_request" in on
    return isinstance(on, dict) and "pull_request" in on


def _resolve(value, contexts):
    """A step env value with its `${{ }}` answered, or (None, the expression it cannot answer)."""
    text = str(value)
    for expr in EXPRESSION.findall(text):
        if expr not in contexts:
            return None, expr
    return EXPRESSION.sub(lambda m: contexts[m.group(1)], text), None


def _env_file(wt, rel):
    """KEY=value lines of a file CI appends to $GITHUB_ENV; comments and blanks skipped."""
    out = {}
    p = Path(wt) / rel
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _step_gates(wt, job, step, loaded_env, contexts, script_prefixes):
    """(gates, why): the gates one step becomes, or why it cannot run here."""
    run = step.get("run") or ""
    if step.get("if"):
        return [], f"CI runs it only when {step['if']}"
    env = dict(loaded_env)
    for scope in (job.get("env") or {}, step.get("env") or {}):
        for k, v in scope.items():
            resolved, missing = _resolve(v, contexts)
            if missing:
                return [], f"its env reads ${{{{ {missing} }}}}, which only CI's runner has"
            env[str(k)] = resolved
    resolved_run, missing = _resolve(run, contexts)
    if missing:
        return [], f"it reads ${{{{ {missing} }}}}, which only CI's runner has"
    cwd = step.get("working-directory") or (job.get("defaults") or {}).get("run", {}).get("working-directory")
    if cwd and "${{" in str(cwd):
        return [], f"its working directory is {cwd}"
    lines = [l.strip() for l in resolved_run.splitlines() if l.strip() and not l.strip().startswith("#")]
    base = {"ci_parity": True, "job": job.get("name") or "", "step": step.get("name") or "", "env": env}
    if cwd:
        base["cwd"] = str(cwd)
    plain = _plain_line(script_prefixes)
    if lines and all(plain.match(l) for l in lines):
        return [dict(base, name=f"ci: {_short_name(l, script_prefixes)}", cmd=shlex.split(l)) for l in lines], None
    unset = sorted({v for v in SHELL_VAR.findall(resolved_run) if v not in env})
    if "$(" in resolved_run or "`" in resolved_run:
        return [], "it runs a command substitution"
    if unset:
        return [], "it reads " + ", ".join(f"${v}" for v in unset) + ", which only CI's runner sets"
    return [dict(base, name=f"ci: {step.get('name') or lines[0]}",
                 cmd=["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", resolved_run])], None


def _short_name(line, script_prefixes):
    """Under a path prefix `python tools/x.py` reads as `tools/x.py`; under a command head
    `npm run lint:css` reads as `lint:css`."""
    words = line.split()
    for p in script_prefixes:
        if p.endswith("/"):
            hit = next((w for w in words if w.startswith(p)), None)
            if hit:
                return hit
            continue
        head = p.split()
        for i in range(len(words) - len(head)):
            if words[i:i + len(head)] == head:
                return words[i + len(head)]
    return words[0]


def derive(wt, default_branch=None, script_prefixes=()):
    """{"gates": [...], "unmeasured": [...]} for the tree at `wt`. Each gate carries `cmd`,
    `env` and `ci_parity`; each unmeasured entry is a gate-shaped row whose `unmeasured`
    names why CI's run of it cannot be repeated here. No prefix, no gate."""
    gates, unmeasured = [], []
    if not script_prefixes:
        return {"gates": gates, "unmeasured": unmeasured}
    invokes = _invokes(script_prefixes)
    contexts = _contexts(default_branch)
    for wf in sorted((Path(wt) / ".github" / "workflows").glob("*.y*ml")):
        try:
            doc = _yaml_load(wf.read_text(encoding="utf-8"))
        except Exception as ex:  # a workflow this reader cannot read is named, never skipped
            unmeasured.append({"name": f"ci: {wf.name}", "true_exit": None, "ci_parity": True, "ci_only": True,
                               "unmeasured": f"the workflow is unreadable here: {ex}"})
            continue
        if not _on_pull_request(doc):
            continue
        for job_id, job in (doc.get("jobs") or {}).items():
            loaded = {}
            for step in (job or {}).get("steps") or []:
                run = (step or {}).get("run") or ""
                for rel in ENV_FILE_LOAD.findall(run):
                    loaded.update(_env_file(wt, rel))
                if not invokes.search(run):
                    continue
                found, why = _step_gates(wt, dict(job, name=job.get("name") or job_id), step, loaded, contexts, script_prefixes)
                if why:
                    unmeasured.append({"name": f"ci: {step.get('name') or job_id}", "true_exit": None,
                                       "ci_parity": True, "ci_only": True, "unmeasured": why})
                gates += found
    return {"gates": gates, "unmeasured": unmeasured}
