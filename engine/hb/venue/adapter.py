"""hb.venue.adapter - the one seam between the engine and a product's venue.

A venue is whatever the product runs for the harness to reach: a sandbox, a dev stack, a local
server. The engine never knows how it is built. The product writes command strings under the
`venue` block of its harness-config.json (docs/config.md), the engine fills the placeholders,
runs them, and reads one JSON line back. What the venue can do is answered by its
`capabilities` command, once per process; a stage whose capability is absent asks nothing and
reads `not offered: <capability>` in the record. A key a declared capability needs and the
config lacks is a refusal before any seat starts.
"""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from hb.rig.common import cfg_or_empty, die, run

CAPABILITIES = ("accounts", "ui", "model", "states", "deploy", "database")
# which command key each capability needs when declared
NEEDS = {"states": ("state_build", "state_check", "state_undo", "states"),
         "accounts": ("sign_in",), "model": ("set_model", "say"), "database": ("database_lease", "database_release"),
         "ui": ("ui",), "deploy": ()}
ALWAYS = ("capabilities", "up", "down", "list")
PLACEHOLDERS = {"capabilities": (), "list": (), "up": ("name", "worktree", "attempt", "options"), "down": ("name", "keep"),
                "state_build": ("state", "name"), "state_check": ("state", "name"), "state_undo": ("state", "name"),
                "sign_in": ("name",), "set_model": ("name", "real"), "say": ("name", "text"), "reload": ("name",),
                "demo_provision": ("slug",), "database_lease": ("slug",), "database_release": ("slug",),
                "memory": ("name",), "purge": ("name",), "prepare_worktree": ("worktree",)}
# the placeholders a call may leave empty; every other one is a value the stage must hand over
OPTIONAL = {"options", "keep"}
_CAPS = {}


def block(c=None):
    """The venue block, or {} when the product declares no venue (the minimum beat) or the tree has no config."""
    return (cfg_or_empty() if c is None else c).get("venue") or {}


def declared(c=None):
    return bool(block(c))


def not_offered(capability):
    return f"not offered: {capability}"


# what each capability's absence leaves unrun, in the record's words
SKIPS = {"ui": "the drive and the pictures", "model": "the battery and the chat audit", "accounts": "sign-in and the demo's venue",
         "states": "the doors and the rehearsal", "deploy": "the deploy watch and the demo", "database": "the test database lease"}


def not_offered_lines(c=None):
    """One line per capability the venue does not offer, naming what ran nothing; the record and the
    PR body print them so a green with no venue reads as the partial measurement it is."""
    if not declared(c):
        return [f"{not_offered('venue')}: no venue block, so " + ", ".join(SKIPS.values()) + " ran nothing"]
    offered = set(capabilities(c))
    return [f"{not_offered(cap)}: {SKIPS[cap]} ran nothing" for cap in CAPABILITIES if cap not in offered]


def _format(token, **values):
    try:
        return token.format(**values)
    except KeyError as ex:
        die(f"venue: the command for this key names a placeholder the engine does not fill: {{{ex.args[0]}}}", 2,
            token=token)
    except (IndexError, ValueError) as ex:
        die(f"venue: a command string has a brace the engine cannot read: {ex}", 2, token=token)


def argv(key, c=None, **values):
    """The command's argv: the template is split first (POSIX, so quotes survive on Windows), then
    each token is filled, so a value with spaces or quotes is one argument and a value's own
    backslashes are kept. An optional placeholder left empty drops its token; an optional one
    holding several words (`{options}`) spreads into as many."""
    template = block(c).get(key)
    if not isinstance(template, str) or not template.strip():
        die(f"venue: no command string under venue.{key} in harness-config.json", 2, key=key)
    for name in PLACEHOLDERS.get(key, ()):
        if name not in values and name not in OPTIONAL:
            die(f"venue.{key}: the engine gave no value for {{{name}}}", 2, key=key)
        values.setdefault(name, "")
    out = []
    for token in shlex.split(template.replace("\\", "/"), posix=True):
        bare = token[1:-1] if token.startswith("{") and token.endswith("}") else None
        if bare in OPTIONAL:
            out.extend(shlex.split(str(values.get(bare) or ""), posix=True))
            continue
        out.append(_format(token, **values))
    return out


def check_config(c=None):
    """Every reason the venue block cannot be run, or []. Called before any seat starts."""
    v = block(c)
    if not v:
        return []
    problems = [f"venue.{k} missing" for k in ALWAYS if not isinstance(v.get(k), str)]
    for key, template in v.items():
        if isinstance(template, str) and key in PLACEHOLDERS:
            allowed = set(PLACEHOLDERS[key])
            import string
            used = {f for _, f, _, _ in string.Formatter().parse(template) if f}
            for f in used - allowed:
                problems.append(f"venue.{key} names a placeholder the engine does not fill: {{{f}}}")
    if problems:
        return problems
    caps = capabilities(c)
    for cap in caps:
        if cap not in CAPABILITIES:
            problems.append(f"venue.capabilities answered an unknown capability: {cap}")
        for key in NEEDS.get(cap, ()):
            if key not in v:
                problems.append(f"venue declares {cap} but has no venue.{key}")
    reuse_table = v.get("reuse")
    if reuse_table is not None:
        if not isinstance(reuse_table, dict):
            problems.append("venue.reuse is not a map of path prefix to keep, reload or rebuild")
        else:
            for prefix, verdict in reuse_table.items():
                if verdict not in ("keep", "reload", "rebuild"):
                    problems.append(f"venue.reuse[{prefix!r}] is {verdict!r}, not keep, reload or rebuild")
            if "reload" in reuse_table.values() and not isinstance(v.get("reload"), str):
                problems.append("venue.reuse maps a path to reload and there is no venue.reload command")
    if "ui" in caps:
        driver = (v.get("ui") or {}).get("driver") if isinstance(v.get("ui"), dict) else None
        if not isinstance(driver, dict) or not driver.get("command"):
            problems.append("venue declares ui but venue.ui.driver has no command")
    return problems


def _last_json(text):
    """The JSON on the last nonempty line of stdout, or None; an earlier line never stands in for it."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except ValueError:
        return None


def call(key, cwd=None, timeout=600, c=None, **values):
    """(rc, json_or_None, text) of one venue command; `c` is the config whose block names it, else the tree's."""
    rc, out, err = run(argv(key, c=c, **values), cwd=str(cwd) if cwd else None, timeout=timeout,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return rc, _last_json(out), (out or "") + (err or "")


def capabilities(c=None):
    """The subset of CAPABILITIES the venue declares, asked once per process and per block."""
    v = block(c)
    if not v:
        return ()
    key = json.dumps(v, sort_keys=True)
    if key not in _CAPS:
        rc, js, text = call("capabilities", timeout=120, c=c)
        if rc != 0 or not isinstance(js, list):
            die("venue: the capabilities command did not answer with one JSON list", 2, rc=rc, tail=text[-300:])
        _CAPS[key] = tuple(str(x) for x in js)
    return _CAPS[key]


def offers(capability, c=None):
    return capability in capabilities(c)


def up(name, worktree, attempt=1, options="", timeout=1800):
    """Stand the venue and return its receipt: origin required, served_sha and tenant_id when the
    venue knows them, facts passed to the briefs unchanged. A receipt without origin is a refusal."""
    rc, js, text = call("up", cwd=worktree, timeout=timeout, name=name, worktree=str(worktree), attempt=str(attempt), options=options)
    if rc != 0:
        return None, f"venue {name}: up exited {rc}: {text[-400:]}"
    if not isinstance(js, dict):
        return None, f"venue {name}: up printed no JSON receipt as its last line: {text[-300:]}"
    if not js.get("origin"):
        return None, f"venue {name}: the receipt has no origin"
    if offers("accounts") and not js.get("tenant_id"):
        return None, "no-tenant-identity: the venue declares accounts and its receipt names no tenant_id"
    return js, None


def down(name, keep=False, timeout=600):
    rc, _js, text = call("down", timeout=timeout, name=name, keep="--keep" if keep else "")
    return rc == 0, text[-300:]


def listed(timeout=120):
    rc, js, text = call("list", timeout=timeout)
    if rc != 0 or not isinstance(js, list):
        die("venue: the list command did not answer with one JSON list", 2, rc=rc, tail=text[-300:])
    return js


def state(verb, state_id, name, timeout=600):
    """verb is build, check or undo. (held_or_ok, text)."""
    rc, _js, text = call(f"state_{verb}", timeout=timeout, state=state_id, name=name)
    return rc == 0, text[-300:]


def states_catalog(c=None):
    """The product's states catalog, read from the path under venue.states; {} without `states`."""
    p = block(c).get("states")
    if not p:
        return {}
    from hb.rig.common import canon
    path = Path(p) if Path(p).is_absolute() else canon() / p
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        die(f"venue.states names {p} and it cannot be read: {ex}", 2)


def sign_in(name, timeout=300):
    """{session, user} or (None, why)."""
    rc, js, text = call("sign_in", timeout=timeout, name=name)
    if rc != 0 or not isinstance(js, dict) or not js.get("session"):
        return None, f"venue.sign_in gave no session: {text[-300:]}"
    return js, None


def set_model(name, real, timeout=300):
    rc, js, text = call("set_model", timeout=timeout, name=name, real="true" if real else "false")
    return rc == 0, (js if isinstance(js, dict) else {"tail": text[-200:]})


def say(name, text_in, timeout=600):
    rc, js, text = call("say", timeout=timeout, name=name, text=text_in)
    return js if rc == 0 and isinstance(js, dict) else {"error": text[-300:]}


def reuse(changed_paths):
    """keep, reload or rebuild for a change set, by the longest matching prefix in venue.reuse;
    rebuild when nothing matches or no table is given, because a venue that cannot say is rebuilt."""
    table = block().get("reuse") or {}
    if not changed_paths:
        return "keep"
    if not isinstance(table, dict) or not table:
        return "rebuild"
    rank = {"keep": 0, "reload": 1, "rebuild": 2}
    worst = "keep"
    for p in changed_paths:
        p = str(p).replace("\\", "/")
        best = max((k for k in table if p.startswith(k)), key=len, default=None)
        verdict = table.get(best, "rebuild") if best else "rebuild"
        if rank.get(verdict, 2) > rank[worst]:
            worst = verdict
    return worst


def reload(name, timeout=600):
    rc, _js, text = call("reload", timeout=timeout, name=name)
    return rc == 0, text[-300:]


def memory_bytes(name):
    """Bytes the venue holds now, or None when the product offers no memory command."""
    if "memory" not in block():
        return None
    rc, js, _ = call("memory", timeout=60, name=name)
    try:
        return int(js.get("bytes")) if rc == 0 and isinstance(js, dict) else None
    except (TypeError, ValueError):
        return None


def purge(name, timeout=600):
    if "purge" not in block():
        return False, "venue.purge not given"
    rc, _js, text = call("purge", timeout=timeout, name=name)
    return rc == 0, text[-300:]


def prepare_worktree(worktree, timeout=600):
    if "prepare_worktree" not in block():
        return True, ""
    rc, _js, text = call("prepare_worktree", cwd=worktree, timeout=timeout, worktree=str(worktree))
    return rc == 0, text[-300:]


def database(verb, slug, timeout=600, cwd=None):
    """verb is lease or release. lease returns the url the product printed."""
    rc, js, text = call(f"database_{verb}", cwd=cwd, timeout=timeout, slug=slug)
    if verb == "lease":
        return (js.get("url") if rc == 0 and isinstance(js, dict) else None), text[-300:]
    return rc == 0, text[-300:]


def ui_driver(session=None, origin=None):
    """The tool spec a tester seat gets, placeholders filled; None without `ui`."""
    spec = (block().get("ui") or {}).get("driver")
    if not spec or not offers("ui"):
        return None
    fill = lambda s: str(s).replace("{session}", str(session or "")).replace("{origin}", str(origin or ""))
    return {"command": fill(spec.get("command", "")), "args": [fill(a) for a in spec.get("args") or []],
            "env": {k: fill(v) for k, v in (spec.get("env") or {}).items()}}
