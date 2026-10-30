"""hb.run.doors - the venue doors a tester can wear, read from the product.

The product's states catalog (`venue.states`, read through `adapter.states_catalog()`, whose
`doors` list names every door by `id`) is the one source, so a new door is one catalog entry
and no engine change. A product with no catalog has no doors the engine can check, and says so.
"""
from hb.venue import adapter


def vocabulary():
    """(doors, why): every door the product's catalog names, or None and why it names none."""
    where = adapter.block().get("states")
    if not where:
        return None, "the product names no states catalog (venue.states)"
    doc = adapter.states_catalog()
    rows = doc.get("doors") if isinstance(doc, dict) else None
    doors = [d["id"] for d in rows or [] if isinstance(d, dict) and isinstance(d.get("id"), str) and d["id"].strip()]
    if not doors:
        return None, f"the states catalog {where} names no doors; `doors` is a list of entries, each with an `id`"
    return doors, None


def check(doc, root=None):
    """Every door problem in a contract/v2 file: a door outside the vocabulary, and an eyes or
    model rule with no `doors` when the file declares `doors_required`. A check without a `root`
    reads nothing from the product, so it judges only the shape."""
    rules = [r for r in doc.get("rules") or [] if isinstance(r, dict)]
    named = [r for r in rules if r.get("doors")]
    required = doc.get("doors_required") is True
    if not named and not required:
        return []
    known, why = vocabulary() if root is not None else (None, None)
    if why:
        return [why]
    said = ", ".join(known or [])
    add_it = f"; a door is added to {adapter.block().get('states')} with the work that builds it"
    problems = []
    for r in rules:
        rid, doors = r.get("id"), r.get("doors")
        if doors is not None and not (isinstance(doors, list) and all(isinstance(d, str) for d in doors)):
            problems.append(f"{rid}: `doors` is a list of venue door names")
            continue
        for d in doors or []:
            if known is not None and d not in known:
                problems.append(f"{rid}: door {d!r} is not a venue door; the venue knows: {said}{add_it}")
        if required and not doors and r.get("proof") in ("eyes", "model"):
            problems.append(f"{rid}: proof {r.get('proof')} names no `doors`, and the contract requires them; "
                            f"the venue knows: {said}")
    return problems
