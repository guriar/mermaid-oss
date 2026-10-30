"""hb.manifest - the contract manifest: read it, subset it, check its schema."""

import re

from hb.rig.yamlmini import _yaml_load
from hb.rig.common import _YamlSubsetError, _item_precedence, _manifest_path, _sha256_file, _slice_key
from hb.venue import adapter


# ── tracked-file locations (§5.3: manifest and ledger are TRACKED, not scratch)
#
# Proof/record artifacts ship TRACKED beside their siblings, never into
# scratch space. The manifest and the delivery ledger are the record of what the
# product is supposed to be and what it actually does, they outlive every wave,
# so they live in the repo, not in .context/. Candidate rows and receipts are
# per-run scratch and stay under state_dir until `ledger fold` promotes them.


# ── the manifest vocabulary (§0, §2.1, §5.4) ─────────────────────────────────

_ITEM_KINDS = ("screen", "form", "list", "chat-verb", "api", "behavior", "measure")
# What holds an item's behavior. `server` = a guard the code
# enforces, driven once. `model` = the model's own choice, filed with a pass rate and
# driven M times. `prompt` (text in the system prompt only) is refused at filing.
_HELD_BY = ("server", "model")

# How an item is proven, ruled at the sitting: `eyes` a verifier drives it in a browser,
# `model` fresh conversations on the real model graded from their transcripts, `test` the
# builder's own test and no verifier. An item nobody can prove is not filed.
_PROOFS = ("eyes", "model", "test")


def proof_of(item):
    """The item's proof: the one it declares, else the one its shape implies (a manifest
    filed before proofs were ruled). A model-held item is proven on the model; an api item
    and one filed not drivable are proven by a test; anything else by eyes."""
    declared = str((item or {}).get("proof") or "").strip()
    if declared in _PROOFS:
        return declared
    if str((item or {}).get("held_by") or "").strip() == "model":
        return "model"
    if (item or {}).get("kind") == "api" or (item or {}).get("drivable") is False:
        return "test"
    return "eyes"



_PRECEDENCE_VALUES = ("artifact-first", "system-first")

_ITEM_REQUIRED = ("id", "rev", "kind", "when", "then", "fixture_recipe_id", "slice")


def _manifest_read(slug, root=None):
    """Returns (doc, err). `err` is a dict ready to `out(..., 2)`.

    THREE outcomes, never two: parsed · absent · unparseable. `absent` and
    `unparseable` are different facts and callers must be able to tell them
    apart, an absent manifest may be a pre-v8 slice; an unparseable one is a
    file this reader could not measure, which is not the same as a bad file."""
    p = _manifest_path(slug, root)
    if not p.exists():
        # a wave whose contract is rules with states (hb.run.rules) is read through the same
        # door, one item per rule, so the ledger and every older reader see one contract
        from hb.contract import rules
        ruled, why = rules.read(slug, p.parent.parent.parent)
        if why:
            return None, {"error": why, "verdict": "manifest-unparseable", "path": str(rules.path_for(slug, p.parent.parent.parent))}
        if ruled is not None:
            return {"schema": "manifest/v1", "wave": ruled.get("wave"), "items": rules.as_items(ruled),
                    "fixture_recipes": [{"id": x.get("id"), "steps": x.get("steps") or []} for x in ruled.get("states") or []],
                    "rulings": ruled.get("rulings") or [], "precedence": {"default": "system-first"},
                    "_path": ruled["_path"], "_sha256": ruled["_sha256"], "_rules": True}, None
    if not p.exists():
        return None, {"error": f"no manifest at {p}, this wave has no contract "
                               "manifest on disk. ABSENT is not the same as EMPTY: "
                               "nothing here says the wave has no items, only that "
                               "no file declares them (§5.3).",
                      "verdict": "manifest-absent", "path": str(p), "slug": slug}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as ex:
        return None, {"error": f"manifest at {p} is UNREADABLE ({ex}), unreadable "
                               "is UNMEASURED, never a pass",
                      "verdict": "manifest-unreadable", "path": str(p)}
    try:
        doc = _yaml_load(text)
    except _YamlSubsetError as ex:
        return None, {"error": f"manifest at {p} could not be PARSED by hb's YAML "
                               f"subset reader: {ex}. This is 'never measured', not "
                               "'invalid', the reader refuses rather than guess at "
                               "a construct it does not implement, because every "
                               "gate downstream keys off the item list it would "
                               "have produced.",
                      "verdict": "manifest-unparseable", "path": str(p)}
    if not isinstance(doc, dict):
        return None, {"error": f"manifest at {p} is not a mapping at the top level "
                               f"(got {type(doc).__name__})",
                      "verdict": "manifest-malformed", "path": str(p)}
    doc["_path"] = str(p)
    doc["_sha256"] = _sha256_file(p)
    return doc, None


def _recipe_library():
    """The state ids the product's states catalog (`venue.states`) carries, or None when the
    product names no catalog (an absent catalog is not an empty one)."""
    if not adapter.block().get("states"):
        return None
    doc = adapter.states_catalog()
    return {str(x["id"]) for x in (doc.get("states") if isinstance(doc, dict) else None) or []
            if isinstance(x, dict) and x.get("id")}


def _manifest_items(doc):
    items = doc.get("items")
    return items if isinstance(items, list) else []


def _manifest_subset(doc, slice_id):
    key = _slice_key(slice_id)
    return [it for it in _manifest_items(doc)
            if isinstance(it, dict) and _slice_key(it.get("slice")) == key]


def _manifest_schema_check(doc):
    """Per-item schema, per §5.3/§5.4. Returns (problems, notes, items).

    Every violation NAMES the item and the observed value. An item that cannot be
    read at all is reported as unreadable, never dropped from the count, because
    a shrinking item list is the exact failure the manifest exists to make
    visible (§0: 'an item that vanishes anywhere downstream is now a visible hole
    in a fixed list')."""
    problems, notes = [], []
    if doc.get("schema") != "manifest/v1":
        problems.append(f"top-level `schema` is {doc.get('schema')!r}, expected "
                        "'manifest/v1', the reader will not assume a shape the "
                        "file does not declare")
    if not doc.get("wave"):
        problems.append("top-level `wave` is missing, the manifest does not say "
                        "which wave it governs")
    prefix = doc.get("id_prefix")
    if "id_prefix" not in doc or prefix is None:
        problems.append("top-level `id_prefix` is missing, so the wave-prefixed-ID "
                        "rule is UNMEASURABLE here, this is 'never measured', not "
                        "'the IDs are fine'. Declare the prefix the sitting minted "
                        "(e.g. id_prefix: MA), or declare it EXPLICITLY EMPTY "
                        "(id_prefix: \"\") when the sitting minted per-surface "
                        "prefixes, rather than letting the reader guess one from "
                        "the slug.")
    elif not str(prefix):
        # Absent, empty and set are THREE states (CLAUDE.md). An explicitly empty
        # prefix is a MEASURED declaration: the sitting minted per-surface prefixes
        # (sales-refound: SY/AP/PU/HM/RL/PH/DT; polish-runway: same shape), so the
        # wave-wide prefix rule is waived by the manifest, not unmeasured.
        notes.append("id_prefix is declared EXPLICITLY EMPTY, the wave-wide prefix "
                     "rule is waived by the manifest (per-surface prefixes); item "
                     "IDs are still checked for presence and uniqueness")
    prec = doc.get("precedence")
    if not isinstance(prec, dict) or not prec.get("default"):
        problems.append("`precedence.default` is not declared, §1.1 rules "
                        "precedence is a MANIFEST FIELD; an undeclared default "
                        "degrades into a builder's impression, which is the exact "
                        "state the field exists to remove")
    elif prec.get("default") not in _PRECEDENCE_VALUES:
        problems.append(f"`precedence.default` is {prec.get('default')!r}, not one "
                        f"of {_PRECEDENCE_VALUES}")
    items = _manifest_items(doc)
    if not items:
        problems.append("`items` is empty or absent, a manifest with no items "
                        "cannot be distinguished from a wave with no contract, and "
                        "every gate downstream would key off nothing")
    # Ruled 2026-09-05: `fixture_recipe_id` was a sentence pointing at nothing, so the
    # verifier invented the route live and every false red of the accounts wave was a
    # route, not the product. The sitting defines every recipe as plain steps here;
    # the verifier writes the concrete route against them at the top of its drive.
    recipes = doc.get("fixture_recipes")
    recipe_steps = {}
    if recipes is None:
        problems.append("top-level `fixture_recipes` is missing, every "
                        "fixture_recipe_id must resolve to a recipe with steps "
                        "(ruled 2026-09-05). Shape: fixture_recipes: [{id: \"<the id "
                        "the items name>\", steps: [\"open /join\", \"...\"]}]")
    elif not isinstance(recipes, list):
        problems.append(f"`fixture_recipes` is {type(recipes).__name__}, not a list")
    else:
        for k, rcp in enumerate(recipes):
            if not isinstance(rcp, dict) or not str(rcp.get("id") or "").strip():
                problems.append(f"fixture_recipes[{k}] has no `id`")
                continue
            steps = rcp.get("steps")
            if not isinstance(steps, list) or not [x for x in steps if str(x).strip()]:
                problems.append(f"fixture_recipes[{k}] {rcp.get('id')!r}: `steps` is "
                                "empty or not a list, a recipe with no steps is the "
                                "sentence this rule retires")
                continue
            recipe_steps[str(rcp["id"]).strip()] = [str(x) for x in steps]
            # a recipe whose steps send text through the reader or the judge says so, and
            # the drive runs the real model for every item on it
            if rcp.get("model") is not None and rcp.get("model") not in ("real", "fake"):
                problems.append(f"fixture_recipes[{k}] {rcp.get('id')!r}: `model` is {rcp.get('model')!r}, "
                                "not real or fake")
    # Ruling 12: a step written `recipe: <name>` names a venue state from the product's
    # states catalog, built by name through product doors. A name the catalog does not
    # carry is a problem; a manifest recipe with no such step relies on improvised setup
    # and is NAMED here, never refused, so the owner sees the count at signing.
    library = _recipe_library()
    improvised = []
    for rid, steps in recipe_steps.items():
        named = [re.sub(r"^recipe:\s*", "", st).split()[0] for st in steps
                 if re.match(r"^recipe:\s*\S", st)]
        for nm in named:
            if library is not None and nm not in library:
                problems.append(f"fixture recipe {rid!r} names `recipe: {nm}`, which "
                                f"the venue states catalog does not carry (it has {sorted(library)[:12]})")
        if not named:
            improvised.append(rid)
    if improvised:
        notes.append(f"{len(improvised)} fixture recipe(s) rely on improvised setup (no "
                     f"`recipe: <name>` step): {improvised[:10]}"
                     + ("" if library is not None else "; the product names no states catalog"))
    seen = {}
    for n, it in enumerate(items):
        if not isinstance(it, dict):
            problems.append(f"items[{n}] is not a mapping (got "
                            f"{type(it).__name__}), it is COUNTED and refused, "
                            "never skipped")
            continue
        iid = str(it.get("id") or "")
        where = iid or f"items[{n}]"
        _rid = str(it.get("fixture_recipe_id") or "").strip()
        if _rid and recipes is not None and _rid not in recipe_steps:
            problems.append(f"{where}: fixture_recipe_id {_rid!r} names no entry in "
                            "`fixture_recipes`, the verifier has no steps to build "
                            "this item's situation from (ruled 2026-09-05)")
        for f in _ITEM_REQUIRED:
            v = it.get(f)
            if v is None or (isinstance(v, str) and not v.strip()):
                if f == "slice":
                    continue     # the unassigned-ID check below owns this one
                problems.append(f"{where}: required field `{f}` is missing/empty")
        if not iid:
            problems.append(f"items[{n}]: no `id`")
            continue
        if iid in seen:
            problems.append(f"{iid}: DUPLICATE id (also at items[{seen[iid]}]), "
                            "IDs are the join key for every gate; two rows with "
                            "one id make one of them invisible")
        seen[iid] = n
        if prefix and not iid.startswith(str(prefix)):
            problems.append(f"{iid}: does not carry the wave prefix "
                            f"{str(prefix)!r}")
        rev = it.get("rev")
        if not isinstance(rev, int):
            problems.append(f"{iid}: `rev` is {rev!r}, not an integer, a ledger "
                            "row binds to item@rev and a non-integer rev cannot be "
                            "compared")
        kind = it.get("kind")
        if kind not in _ITEM_KINDS:
            problems.append(f"{iid}: `kind` is {kind!r}, not one of {_ITEM_KINDS} "
                            "- kind decides the §5.4 edge battery, so an unknown "
                            "kind means the drive set is unknowable")
        held = it.get("held_by")
        if held is not None and held not in _HELD_BY:
            problems.append(f"{iid}: `held_by` is {held!r}, not one of {_HELD_BY}. "
                            "An item held only by prompt text is refused at filing "
                            ": four defects across two waves had "
                            "that one cause. Name the server-side guard (held_by: "
                            "server) or file it as a model item with a pass rate.")
        if held == "model":
            pr_ = str(it.get("pass_rate") or "").strip()
            m_ = re.fullmatch(r"(\d+)/(\d+)", pr_)
            if not m_ or int(m_.group(1)) > int(m_.group(2)) or int(m_.group(2)) < 1:
                problems.append(f"{iid}: held_by model needs `pass_rate: \"N/M\"` (got "
                                f"{pr_!r}); its battery's pass^k is graded against N of M "
                                "(hb.run.battery)")
        proof = it.get("proof")
        if proof is not None and proof not in _PROOFS:
            problems.append(f"{iid}: `proof` is {proof!r}, not one of {_PROOFS}")
        if proof == "model" and held != "model":
            problems.append(f"{iid}: proof model needs `held_by: model` and a pass_rate; a guard the "
                            "server enforces is proven by eyes or by a test")
        if proof == "model" and not it.get("say"):
            problems.append(f"{iid}: proof model needs `say`: the user's words, verbatim, that the "
                            "golden cases and the tester's discovery start from")
        if proof in ("eyes", "model") and it.get("drivable") is False:
            problems.append(f"{iid}: filed not drivable with proof {proof}; an item nobody can drive "
                            "is proven by a test or is not filed")
        if kind == "chat-verb" and held is None:
            problems.append(f"{iid}: a chat-verb item declares `held_by`: server (a "
                            "guard the code enforces, driven once) or model (with a "
                            "pass_rate, graded by its battery). Without it nobody can say "
                            "whether a miss is a defect or a dice roll.")
        if not str(it.get("fixture_recipe_id") or "").strip():
            problems.append(f"{iid}: no `fixture_recipe_id`, §5.4 makes the "
                            "one-line fixture the seeder's input; without it this "
                            "item's drive is a future UNMEASURED/stack-failed row, "
                            "not a green-or-red one")
        p_val, p_src = _item_precedence(doc, it)
        if p_val is None:
            problems.append(f"{iid}: precedence resolves NOWHERE (no item field, no "
                            "surface declaration, no manifest default), it is left "
                            "UNDECLARED rather than defaulted, because defaulting an "
                            "authority field is the honesty-parameter defect")
        elif p_val not in _PRECEDENCE_VALUES:
            problems.append(f"{iid}: precedence {p_val!r} (from {p_src}) is not one "
                            f"of {_PRECEDENCE_VALUES}")
    return problems, notes, items
