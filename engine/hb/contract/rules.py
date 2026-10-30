"""hb.run.rules - the contract as rules with states.

A sitting's contract is a data file, `docs/manifests/<wave>.json`, written by an agent and
checked here, never edited by hand:

    {"schema": "contract/v2", "wave": "<slug>",
     "rulings": ["..."],
     "studio": {"branch": "studio/<slug>", "commit": "<sha>"},   # optional: the frozen screens
     "doors_required": true,                                      # optional: set by the sitting
     "states": [{"id": "one-open-draft", "says": "one draft is open", "steps": ["..."]}],
     "crops":  [{"id": "role-drawer", "path": "design/<slug>/crops/role-drawer.png", "sha256": "..."}],
     "slices": [{"key": "one-answer", "landing": 1, "gets": "what a manager gets, one sentence",
                 "whole_because": "optional: why the slice is one territory, read by the owner on the card"}],
     "rules":  [{"id": "R-CB-7", "rev": 1, "slice": "one-answer", "says": "one sentence the owner signs",
                 "proof": "eyes|model|test", "states": ["all"], "crop": null, "doors": ["sales"],
                 "cases": [{"id": "CB-08", "when": "...", "then": "...", "boundary": "screen|wire|both",
                            "after_compose": false}]}]}

The owner signs rules: one sentence each, the states it must hold in, and how it is
proven. The cases under a rule are what a tester or a test checks, state by state. A crop
is a picture of one part of the demo, cited only where the look of that part is the
decision; the demo itself is never a builder's input (the product as it stands is where a
build starts). `studio` names the frozen studio commit a slice branch starts from; `doors`
are the venue states (hb.contract.doors, read from the product's states catalog) a tester wears
for the rule, required of every eyes and model rule once `doors_required` is set. A file that fails `check` is a problem a run stops
on before any seat.

A case names the `boundary` its check asserts at (hb.run.checks) and may carry `after_compose:
true` when it needs a sibling slice's work (A18). A file where no case names a boundary was
written before checks existed: it reads as `contract/v1 cases, no checks`, reported, never refused.
"""
import hashlib
import json
import re
from pathlib import Path

SCHEMA = "contract/v2"
PROOFS = ("eyes", "model", "test")
EVERY_STATE = "all"
BOUNDARIES = ("screen", "wire", "both")
CASES_WITHOUT_CHECKS = "contract/v1 cases, no checks"
STUDIO_BRANCH = re.compile(r"studio/[\w.-]+")
STUDIO_COMMIT = re.compile(r"[0-9a-f]{7,40}")


def path_for(slug, root):
    return Path(root) / "docs" / "manifests" / f"{slug}.json"


def read(slug, root):
    """(doc, problem): the parsed file, or why it could not be read. Absent is (None, None)."""
    p = path_for(slug, root)
    if not p.exists():
        return None, None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        return None, f"{p} could not be read as JSON: {ex}"
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return None, f"{p} is not a {SCHEMA} file"
    doc["_path"] = str(p)
    doc["_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    return doc, None


def check(doc, root=None):
    """Every reason the file cannot be signed or run, as plain sentences."""
    problems = []
    states = {s.get("id"): s for s in doc.get("states") or [] if isinstance(s, dict)}
    crops = {c.get("id"): c for c in doc.get("crops") or [] if isinstance(c, dict)}
    slices = {s.get("key"): s for s in doc.get("slices") or [] if isinstance(s, dict)}
    if not str(doc.get("wave") or "").strip():
        problems.append("no `wave`")
    problems += studio_problems(doc.get("studio"))
    for key, s in slices.items():
        if not str(s.get("gets") or "").strip():
            problems.append(f"slice {key}: no `gets`, the one sentence saying what a manager gets")
    for cid, c in crops.items():
        if root is not None:
            f = Path(root) / str(c.get("path") or "")
            if not f.is_file():
                problems.append(f"crop {cid}: {c.get('path')} is not in the tree")
            elif c.get("sha256") and hashlib.sha256(f.read_bytes()).hexdigest() != c["sha256"]:
                problems.append(f"crop {cid}: {c.get('path')} changed since it was pinned")
    seen_rules, seen_cases, per_slice = set(), {}, {}
    bounded = cases_era(doc) != CASES_WITHOUT_CHECKS
    for n, r in enumerate(doc.get("rules") or []):
        if not isinstance(r, dict) or not str(r.get("id") or "").strip():
            problems.append(f"rules[{n}]: no id")
            continue
        rid = r["id"]
        if rid in seen_rules:
            problems.append(f"{rid}: two rules share this id")
        seen_rules.add(rid)
        if not isinstance(r.get("rev"), int):
            problems.append(f"{rid}: `rev` is not a whole number")
        says = str(r.get("says") or "").strip()
        if not says:
            problems.append(f"{rid}: no `says`")
        if r.get("slice") not in slices:
            problems.append(f"{rid}: slice {r.get('slice')!r} is not in `slices`")
        per_slice[r.get("slice")] = per_slice.get(r.get("slice"), 0) + 1
        if r.get("proof") not in PROOFS:
            problems.append(f"{rid}: `proof` is {r.get('proof')!r}, not one of {PROOFS}; a rule nobody can prove is not filed")
        named = r.get("states") or []
        if not named:
            problems.append(f"{rid}: no `states`; name the states it holds in, or [\"{EVERY_STATE}\"]")
        for s in named:
            if s != EVERY_STATE and s not in states:
                problems.append(f"{rid}: state {s!r} is not in `states`")
        if r.get("proof") == "model" and not (r.get("say") and re.fullmatch(r"\d+/\d+", str(r.get("pass_rate") or ""))):
            problems.append(f"{rid}: proof model needs `say` (the manager's words) and `pass_rate` \"N/M\"")
        if r.get("crop") and r["crop"] not in crops:
            problems.append(f"{rid}: crop {r['crop']!r} is not in `crops`")
        if not r.get("cases"):
            problems.append(f"{rid}: no cases; a rule with nothing to check proves nothing, write at least one when and then")
        for c in r.get("cases") or []:
            cid = (c or {}).get("id")
            if not cid or not str(c.get("when") or "").strip() or not str(c.get("then") or "").strip():
                problems.append(f"{rid}: a case needs an id, a when and a then")
            elif cid in seen_cases:
                problems.append(f"{cid}: a case of both {seen_cases[cid]} and {rid}; a case sits under one rule")
            else:
                seen_cases[cid] = rid
            if bounded:
                problems += case_boundary_problems(rid, c or {})
    for key in slices:
        if key not in per_slice:
            problems.append(f"slice {key}: no rule names it")
    from hb.contract import doors
    return problems + doors.check(doc, root)


def cases_era(doc):
    """`contract/v2` when any case names a boundary, else the older cases a check cannot run."""
    for r in doc.get("rules") or []:
        for c in (r.get("cases") or []) if isinstance(r, dict) else []:
            if isinstance(c, dict) and c.get("boundary") is not None:
                return SCHEMA
    return CASES_WITHOUT_CHECKS


def case_boundary_problems(rid, case):
    """Why one case cannot carry a check: every case names where its check asserts (A18)."""
    cid = case.get("id")
    out = []
    if case.get("boundary") not in BOUNDARIES:
        out.append(f"{rid} case {cid}: `boundary` is {case.get('boundary')!r}; every case names where its check "
                   f"asserts, one of {', '.join(BOUNDARIES)}")
    if "after_compose" in case and not isinstance(case["after_compose"], bool):
        out.append(f"{rid} case {cid}: `after_compose` is true or false")
    return out


def studio_problems(studio):
    """`studio` is absent, or {"branch": "studio/<slug>", "commit": 7 to 40 hex}."""
    if studio is None:
        return []
    if not isinstance(studio, dict):
        return ["`studio` is {\"branch\": \"studio/<slug>\", \"commit\": \"<sha>\"}"]
    out = []
    if not STUDIO_BRANCH.fullmatch(str(studio.get("branch") or "")):
        out.append(f"studio: branch {studio.get('branch')!r} is not studio/<slug>")
    if not STUDIO_COMMIT.fullmatch(str(studio.get("commit") or "")):
        out.append(f"studio: commit {studio.get('commit')!r} is not a sha of 7 to 40 hex characters")
    return out


def as_items(doc, slice_key=None, listed=None):
    """The rules of one slice (or the ids `listed`) in the shape a run's contract carries:
    one item per rule, its cases and states beside it."""
    states = {s.get("id"): s for s in doc.get("states") or [] if isinstance(s, dict)}
    crops = {c.get("id"): c for c in doc.get("crops") or [] if isinstance(c, dict)}
    rows = []
    for r in doc.get("rules") or []:
        if listed and r.get("id") not in listed:
            continue
        if not listed and slice_key and r.get("slice") != slice_key:
            continue
        named = list(r.get("states") or [])
        where = "every venue state" if EVERY_STATE in named else \
            "the venue is in: " + "; ".join(str(states.get(s, {}).get("says") or s) for s in named)
        rows.append({"id": r["id"], "rev": r.get("rev") or 1, "slice": r.get("slice"), "when": where, "then": r.get("says"),
                     "kind": "chat-verb" if r.get("proof") == "model" else "behavior", "proof": r.get("proof"),
                     "held_by": "model" if r.get("proof") == "model" else "server",
                     "pass_rate": r.get("pass_rate"), "battery": r.get("battery"), "k": r.get("k"),
                     "say": r.get("say"), "files": r.get("files") or [],
                     # every rule of one state rides one tester, who builds that state once
                     "fixture_recipe_id": "+".join(sorted(named)) or None,
                     "states": named, "cases": r.get("cases") or [], "doors": r.get("doors") or [],
                     "crop": (crops.get(r.get("crop")) or {}).get("path")})
    return rows


def states_for(doc, items):
    """The definitions of the states these items name, for a tester's brief."""
    used = {s for it in items for s in it.get("states") or []}
    return [s for s in doc.get("states") or [] if isinstance(s, dict) and (s.get("id") in used or EVERY_STATE in used)]


def card(doc):
    """The filing card: what the owner reads and signs, slice by slice."""
    lines = [f"# {doc.get('wave')}: the filing card", ""]
    rules = doc.get("rules") or []
    for s in sorted(doc.get("slices") or [], key=lambda x: (x.get("landing") or 0, str(x.get("key")))):
        mine = [r for r in rules if r.get("slice") == s.get("key")]
        by = {p: sum(1 for r in mine if r.get("proof") == p) for p in PROOFS}
        lines += [f"## {s.get('key')}" + (f" (landing {s['landing']})" if s.get("landing") else ""), "",
                  str(s.get("gets") or ""), "",
                  f"{len(mine)} rules: {by['eyes']} by eyes, {by['model']} on the model, {by['test']} by a test."
                  + (f" Kept whole because {s['whole_because']}" if s.get("whole_because") else ""), "",
                  "| Rule | Says | States | Proof | Cases |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {r['id']} | {r.get('says')} | {', '.join(r.get('states') or [])} | {r.get('proof')} | {len(r.get('cases') or [])} |"
                  for r in mine]
        lines.append("")
    return "\n".join(lines)


def cmd_contract(a):
    """hb contract check|card --wave <slug>: is the file signable, and what the owner signs."""
    from hb.rig.common import doc_root, out
    root = doc_root()
    doc, why = read(a.wave, root)
    if doc is None:
        out({"verdict": "unreadable" if why else "absent", "why": why or f"no {path_for(a.wave, root)}"}, 2)
    problems = check(doc, root)
    if a.op == "card":
        print(card(doc))
    rules = doc.get("rules") or []
    out({"verdict": "signable" if not problems else "problems", "problems": problems, "rules": len(rules),
         "slices": len(doc.get("slices") or []), "cases": sum(len(r.get("cases") or []) for r in rules),
         "cases_era": cases_era(doc),
         "proofs": {p: sum(1 for r in rules if r.get("proof") == p) for p in PROOFS}}, 0 if not problems else 1)


def register(sub):
    s = sub.add_parser("contract", help="a rules-with-states contract: check it is signable, or print its filing card")
    s.add_argument("op", choices=("check", "card"))
    s.add_argument("--wave", required=True)
