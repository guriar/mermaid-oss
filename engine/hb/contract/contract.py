"""hb.run.contract - the one contract behind an issue number.

A slice always opens by issue number. Behind it stands one of three things, and the
resolver reads whichever is there, in this order:

  1. a manifest the issue body's `## Items` section names (`<paths.manifest_dir>/<slug>.yaml`,
     slice `<key>`), written by a design sitting or by a chat that filed a slice;
  2. `when:` / `then:` lines in the issue body's `## Items` section;
  3. a hotfix folder the issue body points at (`<paths.hotfix_dir>/<slug>/contract.md`),
     a hotfix that grew.

A sentence resolves only for the hotfix door; `resolve(ref, root, require_issue=True)`
refuses it, and an issue with none of the three is a problem, never a fabricated item.

All three end as the same shape, so the builder cannot tell which door the contract
came through:

  {"issue": "#N", "goal": "...", "source": "manifest|hotfix-folder|issue-body|sentence",
   "items": [{"item": "XX-01", "when": "...", "then": "...", "kind": "..."}],
   "body": "<the issue body, verbatim>", "rulings": "<verbatim>",
   "checks": [...], "problems": [...]}

`rulings` is the issue's `## Rulings` section and every ruling the manifest carries, whole.
`problems` non-empty means the contract is stale against the tree (a mock moved, a
named file is gone, an item has no when or then) and the run stops in one line before
any seat starts.
"""
import re
from pathlib import Path

from hb.rig.common import _P, _verify_artifacts, canon, gh
from hb.contract import rules
from hb.record import success
from hb.contract.manifest import _manifest_items, _manifest_read, _manifest_schema_check, _manifest_subset, proof_of

def _section(body, name):
    m = re.search(rf"^##\s+{re.escape(name)}\s*\n(.*?)(?=^##\s|\Z)", body or "", re.S | re.M)
    return m.group(1).strip() if m else ""


def _shape(item_id, when, then, kind=None, raw=None):
    raw = raw or {}
    it = {"item": str(item_id), "when": str(when or "").strip(), "then": str(then or "").strip(),
          "kind": kind or raw.get("kind") or "behaviour",
          "files": raw.get("files") or raw.get("touches") or raw.get("paths") or [],
          "fixture_recipe_id": raw.get("fixture_recipe_id"),
          # filed grey: not drivable on any stand-in, so UNMEASURED by nature never blocks
          "drivable": raw.get("drivable", True) is not False,
          # the words' revision: the ledger books it and a re-drive names the rev it graded
          "rev": raw.get("rev") or 1,
          # the model the item's path runs, from its fixture recipe
          "model": raw.get("model"),
          # who holds the behaviour; a model-held item is graded by its battery (hb.run.battery)
          "held_by": raw.get("held_by"),
          "pass_rate": raw.get("pass_rate"),
          # the golden case set that grades the item, and tries per case when the item names them (hb.run.battery)
          "battery": raw.get("battery"), "k": raw.get("k"),
          "say": raw.get("say"),
          # a rule's cases, the states it holds in, and the one picture it cites (hb.run.rules)
          "cases": raw.get("cases") or [], "states": raw.get("states") or [], "crop": raw.get("crop"),
          # the venue doors a tester wears for this item (hb.run.doors)
          "doors": raw.get("doors") or [],
          "mock": raw.get("mock") or raw.get("artifact")}
    it["proof"] = proof_of({**raw, **it})
    return it


def _listed_ids(section):
    """The ids an `## Items` section names, ranges expanded: `T-01..T-04, T-07`."""
    ids = []
    for pre, a, b in re.findall(r"\b([A-Z]{1,4})-(\d{2,3})\.\.(?:[A-Z]{1,4}-)?(\d{2,3})\b", section):
        width = len(a)
        ids += [f"{pre}-{n:0{width}d}" for n in range(int(a), int(b) + 1)]
    rest = re.sub(r"[A-Z]{1,4}-\d{2,3}\.\.(?:[A-Z]{1,4}-)?\d{2,3}", " ", section)
    for full in re.findall(r"\b([A-Z]{1,4}-\d{2,3})\b", rest):
        if full not in ids:
            ids.append(full)
    return ids



def _folder(key):
    """A configured folder (`paths.<key>`) as a slash path with no trailing slash."""
    return str(_P(key)).replace("\\", "/").rstrip("/")

def _from_manifest(body, root):
    """The filing shape (docs/agents/slicing.md): `## Items` is the id list, the trailer
    names the manifest and the slice key. An older body names both inside `## Items`."""
    section = _section(body, "Items") or ""
    folder = _folder("manifest_dir")
    named = re.escape(folder) + r"/([\w.-]+?)\.(?:ya?ml|json)"
    m = re.search(named, section) or re.search(r"\*\*MANIFEST:\*\*[^\n]*?" + named, body or "")
    if not m:
        return None
    slug = m.group(1)
    ruled = _from_rules(slug, body, section, root)
    if ruled is not None:
        return ruled
    where = section if f"{folder}/" in section else (body or "")
    sm = re.search(r"\bslice\s*[:`]?\s*`?([\w.-]+)`?", where, re.I)
    slice_key = sm.group(1).rstrip(".") if sm else None
    listed = _listed_ids(section)
    doc, err = _manifest_read(slug, root)
    if err:
        return {"source": "manifest", "items": [], "problems": [err.get("error")], "checks": [],
                "manifest": f"{folder}/{slug}.yaml", "slice": slice_key}
    problems, notes, _items = _manifest_schema_check(doc)
    if problems:
        # A manifest that fails its own schema is stale by definition; nothing is derived
        # from it, so the report names the schema problems and not a half-read contract.
        return {"source": "manifest", "items": [], "problems": [str(x) for x in problems], "checks": [],
                "manifest": doc.get("_path"), "slice": slice_key}
    raw_items = _manifest_subset(doc, slice_key) if slice_key else _manifest_items(doc)
    if slice_key and not raw_items:
        # the slice key is a label; the id list in `## Items` is the authority
        raw_items = _manifest_items(doc)
    if listed:
        by_id = {str(it.get("id")): it for it in raw_items if isinstance(it, dict)}
        missing = [i for i in listed if i not in by_id]
        if missing:
            problems.append(f"`## Items` names {missing} and manifest {slug} carries no such item")
        raw_items = [by_id[i] for i in listed if i in by_id]
    if not raw_items:
        problems.append(f"manifest {slug} yields no items for this issue")
    items = []
    recipe_model = {str(r.get("id")): r.get("model") for r in doc.get("fixture_recipes") or [] if isinstance(r, dict)}
    for k, it in enumerate(raw_items):
        if not isinstance(it, dict):
            problems.append(f"manifest {slug} items[{k}] is not a mapping")
            continue
        if not str(it.get("id") or "").strip():
            problems.append(f"manifest {slug} items[{k}] has no id")
            continue
        it = dict(it, model=it.get("model") or recipe_model.get(str(it.get("fixture_recipe_id"))))
        items.append(_shape(it.get("id"), it.get("when"), it.get("then"), it.get("kind"), it))
    art_rows, art_problems = _verify_artifacts(doc, raw_items, root)
    checks = [{"artifact": r.get("path"), "verdict": r.get("verdict") or r.get("status"),
               "basis": r.get("match_basis")} for r in art_rows or [] if isinstance(r, dict)]
    problems += [str(p) for p in art_problems or []]
    return {"source": "manifest", "items": items, "problems": problems, "checks": checks,
            "notes": notes, "manifest": doc.get("_path"), "manifest_sha256": doc.get("_sha256"),
            "slice": slice_key, "mocks": [r.get("path") for r in art_rows or [] if isinstance(r, dict)]}


def _from_rules(slug, body, section, root):
    """The slice's rules from a rules-with-states file (hb.run.rules), or None when the wave
    has no such file and the older items manifest is the contract."""
    doc, why = rules.read(slug, root)
    if doc is None and why is None:
        return None
    if doc is None:
        return {"source": "manifest", "items": [], "problems": [why], "checks": [], "manifest": f"{_folder('manifest_dir')}/{slug}.json"}
    where = section if f"{_folder('manifest_dir')}/" in section else (body or "")
    sm = re.search(r"\bslice\s*[:`]?\s*`?([\w.-]+)`?", where, re.I)
    slice_key = sm.group(1).rstrip(".") if sm else None
    problems = rules.check(doc, root)
    # the manifest's slice carve is the one source of which rules a slice builds; the issue's
    # `## Items` line is the sitting's prose for the builder to read, never parsed
    ruled = [r for r in doc.get("rules") or [] if isinstance(r, dict)]
    carve = sorted(r.get("id") for r in ruled if slice_key and r.get("slice") == slice_key)
    if not slice_key:
        problems.append(f"the issue names no slice of {doc['_path']}; a rules file is read only by its carve, "
                        "so the issue must name its slice")
    elif not carve:
        keys = sorted({str(r.get("slice")) for r in ruled if r.get("slice")})
        problems.append(f"manifest {doc['_path']} carves nothing for slice {slice_key!r}; it carves {keys}")
    rows = rules.as_items(doc, slice_key, carve) if carve else []
    if not rows and not problems:
        problems.append(f"{doc['_path']} yields no rules for this issue")
    items = [_shape(r["id"], r["when"], r["then"], r["kind"], r) for r in rows]
    return {"source": "manifest", "items": items, "problems": problems, "checks": [], "manifest": doc["_path"],
            "manifest_sha256": doc["_sha256"], "slice": slice_key, "states": rules.states_for(doc, rows),
            "studio": doc.get("studio"), "manifest_rulings": doc.get("rulings") or [],
            "mocks": [r["crop"] for r in rows if r.get("crop")]}


def _from_hotfix_folder(body, root):
    m = re.search(re.escape(_folder("hotfix_dir")) + r"/([\w.-]+)/", body or "")
    if not m:
        return None
    folder = Path(root) / _folder("hotfix_dir") / m.group(1)
    contract = folder / "contract.md"
    if not contract.exists():
        return {"source": "hotfix-folder", "items": [], "checks": [],
                "problems": [f"{contract} is named by the issue and does not exist"]}
    text = contract.read_text(encoding="utf-8")
    items = []
    for mm in re.finditer(r"^-\s*([A-Z]+-\d+)\s+when:\s*(.+?)\n\s+then:\s*(.+?)(?=\n-\s*[A-Z]+-\d+\s+when:|\n\n|\Z)",
                          text, re.M | re.S):
        when = " ".join(mm.group(2).split())
        # an item may close on `proof:` and `say:` lines of its own, as a manifest item does
        raw, kept = {}, []
        for line in mm.group(3).splitlines():
            own = re.match(r"^\s*(proof|say):\s*(.+?)\s*$", line)
            if own:
                raw[own.group(1)] = own.group(2)
            else:
                kept.append(line)
        then = " ".join(" ".join(kept).split())
        items.append(_shape(mm.group(1), when, then, raw=raw))
    problems = [] if items else [f"{contract} carries no `- ID when: ... then: ...` items"]
    head = re.search(r"^#\s*(?:hotfix:\s*)?(.+?)\s*$", text, re.M)
    return {"source": "hotfix-folder", "items": items, "problems": problems, "checks": [],
            "headline": head.group(1) if head else "",
            "folder": str(folder)}


def _from_body(body, goal):
    section = _section(body, "Items") or ""
    pairs = []
    for m in re.finditer(r"^\s*(?:-\s*)?when:\s*(.+?)\n\s*(?:-\s*)?then:\s*(.+?)$", section, re.I | re.M):
        pairs.append((m.group(1).strip(), m.group(2).strip()))
    if not pairs:
        return None
    items = [_shape(f"IS-{i + 1:02d}", w, t) for i, (w, t) in enumerate(pairs)]
    return {"source": "issue-body", "items": items, "problems": [], "checks": []}


def _files_exist(items, root):
    """Every file an item names must exist in the tree; a missing one is a stale premise."""
    problems = []
    for it in items:
        names = it.get("files") or []
        if isinstance(names, str):
            names = [names]
        for f in names:
            f = str(f).split(":")[0].strip().replace("\\", "/")
            if not f:
                continue
            if f.startswith("/") or f.startswith("..") or ":" in f or "/../" in f:
                problems.append(f"{it['item']} names {f}, which is outside the tree")
                continue
            if not (Path(root) / f).exists():
                problems.append(f"{it['item']} names {f}, which is not in the tree")
    return problems


def resolve(ref, root=None, require_issue=False):
    """The contract behind `ref`: `#N`, `N`, or (for the hotfix door only) a sentence."""
    root = Path(root or canon())
    hotfix_dir = _folder("hotfix_dir")
    folder = re.search(r"[\\/]".join(re.escape(part) for part in hotfix_dir.split("/")) + r"[\\/]([\w.-]+)", str(ref))
    if folder and not re.match(r"^#?(\d+)$", str(ref).strip()):
        found = _from_hotfix_folder(f"{hotfix_dir}/{folder.group(1)}/", root) or {}
        items = found.get("items") or []
        goal = items[0]["then"] if items else str(ref)
        return {"issue": f"folder:{folder.group(1)}", "goal": goal, "source": "hotfix-folder", "items": items,
                "headline": found.get("headline") or "",
                "rulings": "", "checks": [],
                "problems": list(found.get("problems") or []) + _files_exist(items, root)}
    m = re.match(r"^#?(\d+)$", str(ref).strip())
    if not m:
        if require_issue:
            return {"issue": "none", "goal": str(ref).strip(), "source": "sentence", "items": [], "rulings": "",
                    "checks": [],
                    "problems": ["the slice door opens by issue number; a sentence is the hotfix door"]}
        goal = str(ref).strip()
        items = [_shape("HF-01", "a person reads or drives what the sentence names", goal)]
        return {"issue": "sentence", "goal": goal, "source": "sentence", "items": items,
                "rulings": "", "checks": [], "problems": []}
    n = m.group(1)
    rc, o, e = gh(["issue", "view", n, "--json", "title,body,labels"])
    if rc != 0:
        return {"issue": f"#{n}", "goal": "", "source": "issue", "items": [], "rulings": "",
                "checks": [], "problems": [f"gh issue view {n} failed: {(e or '').strip()[:200]}"]}
    import json
    try:
        d = json.loads(o or "{}")
    except json.JSONDecodeError as ex:
        return {"issue": f"#{n}", "goal": "", "source": "issue", "items": [], "rulings": "",
                "checks": [], "problems": [f"gh issue view {n} returned no JSON: {ex}"]}
    body = d.get("body") or ""
    goal = d.get("title") or f"#{n}"
    rulings = _section(body, "Rulings")
    success_block = success.parse(_section(body, "Success"))
    found = _from_manifest(body, root) or _from_body(body, goal) or _from_hotfix_folder(body, root)
    if not found:
        return {"issue": f"#{n}", "goal": goal, "source": "issue", "items": [], "rulings": rulings,
                "checks": [],
                "problems": ["the issue names no manifest, carries no when/then items and points at no hotfix folder"]}
    items = found["items"]
    problems = list(found.get("problems") or [])
    for it in items:
        if not it["when"] or not it["then"]:
            problems.append(f"{it['item']} has no when or no then")
    problems += _files_exist(items, root) + success.problems(success_block)
    if found.get("manifest_rulings"):
        rulings = "\n\n".join(x for x in [rulings, "From the manifest:\n" + "\n".join(
            f"- {r}" for r in found["manifest_rulings"])] if x)
    out = {"issue": f"#{n}", "goal": goal, "source": found["source"], "items": items, "body": body,
           "rulings": rulings,
           "checks": found.get("checks") or [], "problems": problems, "success": success_block,
           "labels": [l.get("name") for l in d.get("labels") or [] if isinstance(l, dict)]}
    for k in ("manifest", "manifest_sha256", "slice", "mocks", "folder", "notes", "states", "studio"):
        if found.get(k):
            out[k] = found[k]
    return out


def for_run(j, ref, evidence, root=None, require_issue=False):
    """The contract behind a run's ref, journaled once: resolved, every ruling a docket id in
    the ask, the rulings or an item names quoted beside the rulings, and contract.md written
    to the evidence folder."""
    def do():
        c = resolve(ref, root, require_issue=require_issue)
        named = "\n".join([c["goal"], c.get("rulings") or ""] + [f"{it['when']} {it['then']}" for it in c["items"]])
        quoted = rulings_by_number(named)
        if quoted:
            c["rulings"] = ((c.get("rulings") or "") + "\n\n" if c.get("rulings") else "") + quoted
        (evidence / "contract.md").write_text(render(c), encoding="utf-8")
        c["contract_path"] = str(evidence / "contract.md")
        return c
    return j.op("contract", do)


def rulings_by_number(text):
    """Every `dk-NNNN` in the text, quoted from the docket: title, plain, desired
    state and the owner's verdict note when one exists."""
    from hb.rig.common import _docket_load
    ids = sorted(set(re.findall(r"\bdk-\d+\b", text or "")))
    if not ids:
        return ""
    try:
        by_id = {e.get("id"): e for e in _docket_load().get("entries") or []}
    except Exception:
        return ""
    lines = []
    for i in ids:
        e = by_id.get(i)
        if not e:
            continue
        line = f"- {i} ({e.get('status') or 'open'}): {e.get('title') or ''}. {e.get('plain') or ''}"
        if e.get("desired"):
            line += f" Desired: {e['desired']}"
        note = e.get("verdict_note") or e.get("note")
        if note:
            line += f" Owner: {note}"
        lines.append(line)
    return "\n".join(lines)


def render(contract):
    """contract.md: the items, one line each, for the evidence folder and the brief."""
    lines = [f"# Contract: {contract['goal']}", "", f"issue: {contract['issue']}",
             f"source: {contract['source']}"]
    if contract.get("manifest"):
        lines.append(f"manifest: {contract['manifest']}" + (f" slice {contract['slice']}" if contract.get("slice") else ""))
    if contract.get("studio"):
        lines.append(f"studio: {contract['studio'].get('branch')} at {contract['studio'].get('commit')}")
    lines.append("")
    for it in contract["items"]:
        lines.append(f"- {it['item']} when: {it['when']}")
        lines.append(f"  then: {it['then']}")
        if it.get("files"):
            lines.append(f"  files: {', '.join(str(f) for f in it['files'])}")
        if it.get("doors"):
            lines.append(f"  doors: {', '.join(it['doors'])}")
    if contract.get("checks"):
        lines += ["", "## Mocks against the tree", ""] + [f"- {c.get('artifact')}: {c.get('verdict')}" for c in contract["checks"]]
    if contract.get("success"):
        lines += ["", "## Success", ""] + [f"- {x}" for x in success.lines(contract["success"])]
    if contract.get("rulings"):
        lines += ["", "## Rulings, verbatim", "", contract["rulings"]]
    if contract.get("problems"):
        lines += ["", "## Stale against the tree", ""] + [f"- {p}" for p in contract["problems"]]
    return "\n".join(lines) + "\n"
