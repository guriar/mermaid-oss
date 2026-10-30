"""hb.run.pictures - which pictures a run shows, and the markdown that shows them.

A path on the runner's disk renders as nothing on GitHub. The pictures ride the run's record
commit (hb.run.record) under `evidence/<run_id>/<kind>/<name>.png`, and record.md links each
by its path relative to itself, which GitHub resolves inside the blob view at that commit.

Which pictures: for every item with a screen, one after picture per viewport the product
names in config `pictures.viewports` and the before set when one was taken; for every red
row, what the verifier saw. A picture is named `<item>-<viewport name>.png`, so the readers
below find the viewports on disk and only the capture asks the config.
"""
from pathlib import Path

from hb.contract import policy
from hb.rig.common import cfg, die

RED = ("BROKEN", "UNMEASURED", "DIVERGED")


def viewports():
    """config `pictures.viewports`, a list of {name, width, height}. No default: a capture
    asked for with the key missing is a refusal naming it."""
    listed = (cfg().get("pictures") or {}).get("viewports")
    if not listed or not all(isinstance(v, dict) and v.get("name") and v.get("width") and v.get("height") for v in listed):
        die("pictures.viewports is not in harness-config.json as a list of {name, width, height}; "
            "a picture was asked for and the engine names no viewport of its own", 2)
    return listed


def on_disk(items, evidence, viewport):
    """{item: its before capture at `viewport`} for every item that has one on disk."""
    found = {}
    for it in items or []:
        png = Path(evidence) / "before" / f"{it['item']}-{viewport}.png"
        if png.exists():
            found[it["item"]] = str(png)
    return found


def capture_before(j, a, items, wt, slug, base_sha, evidence, seat_kw, issue):
    """The look of every screen item at the base commit, taken once on one venue by one
    capture seat before anything changes, so the PR can show each screen before and after.
    Journaled as one op: a resume returns the captures the first pass took. Returns
    {item: desktop path}."""
    from hb.runner import seating
    from hb.venue import standin

    def do():
        if a.no_venue or getattr(a, "no_before", False):
            return {}
        screens = [it for it in items or [] if policy.has_screen([it])]
        if not screens:
            j.append("sized", before=False, why="no screen item: no look to capture at the base commit")
            return {}
        shown = viewports()
        vb = standin.venue_up(j, slug, wt, real_llm=False, key="venue-up-base")
        if not vb.get("venue"):
            j.append("venue", state="failed", stage="before", why=vb.get("error"))
            return {}
        vf = standin.venue_facts(vb)
        facts = {"Contract items": screens, "Mode": "before-capture", "The venue": vf,
                 "Evidence": "save each capture under the evidence directory as "
                             + " and ".join(f"before/<ITEM>-{v['name']}.png ({v['width']}x{v['height']})" for v in shown)}
        bs = seating.sit(j, "verify", facts, wt, base_sha, issue, label="verify.before",
                         extra_env={"HB_BROWSER_PROFILE": str(vf.get("browser_profile") or "")}, **seat_kw)
        found = on_disk(items, evidence, shown[0]["name"])
        j.phase("before", captures=found, seat=bs["result"])
        standin.venue_down_keep(j, vb["venue"], wt, "venue-down-base")
        return found
    return j.op("before-captures", do) or {}


def _evidence_of(row, evidence):
    """The evidence folder a row's pictures sit in: a carried row cites its slice's own."""
    cap = Path(str(row.get("capture") or ""))
    if row.get("capture") and cap.is_absolute() and len(cap.parents) >= 2 and cap.parent.name in ("after", "red"):
        return cap.parents[1]
    return Path(evidence)


def collect(evidence, receipts, items):
    """{item: {"after": {screen: path}, "before": {screen: path}, "red": path}} for the
    pictures on disk, in contract order; an item with none is left out."""
    rows = {r.get("item"): r for r in receipts or []}
    ids = [str(it["item"]) for it in items or []]
    out = {}
    for it in items or []:
        i, row = it["item"], rows.get(it["item"]) or {}
        ev = _evidence_of(row, evidence)
        pics = {"after": {}, "before": {}, "red": None}
        if policy.has_screen([it]):
            for kind in ("after", "before"):
                for p in sorted((ev / kind).glob(f"{i}-*.png")) if (ev / kind).is_dir() else []:
                    # item T-1's glob also matches item T-1-b's pictures; those are T-1-b's
                    if not any(len(o) > len(i) and p.stem.startswith(o + "-") for o in ids):
                        pics[kind][p.stem[len(i) + 1:]] = p
        red = ev / "red" / f"{i}.png"
        if row.get("state") in RED and red.is_file():
            pics["red"] = red
        if pics["after"] or pics["before"] or pics["red"]:
            out[i] = pics
    return out


def names(pics):
    """{path in the record: local path}: `after/<item>-<screen>.png`, `before/...`, `red/<item>.png`,
    the same layout the evidence folder keeps, so a carried picture lands under its own name."""
    out = {}
    for item, p in pics.items():
        for s, path in p["after"].items():
            out[f"after/{item}-{s}.png"] = path
        for s, path in p["before"].items():
            out[f"before/{item}-{s}.png"] = path
        if p["red"]:
            out[f"red/{item}.png"] = p["red"]
    return out


def relative(pics):
    """The same shape as `collect`, each path replaced by its address relative to record.md."""
    out = {}
    for item, p in pics.items():
        out[item] = {"after": {s: f"after/{item}-{s}.png" for s in p["after"]},
                     "before": {s: f"before/{item}-{s}.png" for s in p["before"]},
                     "red": f"red/{item}.png" if p["red"] else None}
    return out


def _img(url, alt):
    return f'<img src="{url}" alt="{alt}" width="360">' if url else "none"


def _row(shots, alt):
    """One table: a column per viewport, side by side."""
    names = list(shots)
    return ["| " + " | ".join(names) + " |", "|" + " --- |" * len(names),
            "| " + " | ".join(_img(shots[n], f"{alt} {n}") for n in names) + " |", ""]


def markdown(addr, items, receipts, plain, collapse_over=8):
    """The pictures section: per item its verdict line, the after set side by side, the
    before set when one exists, and what the verifier saw on a red row. Past
    `collapse_over` items each folds under its own details."""
    if not addr:
        return []
    rows = {r.get("item"): r for r in receipts or []}
    fold = len(addr) > collapse_over
    lines = ["## Pictures", ""]
    for it in items or []:
        i = it["item"]
        if i not in addr:
            continue
        p = addr[i]
        head = f"{i} {(rows.get(i) or {}).get('state') or 'not driven'}: {plain(i)}"
        block = []
        if p["after"]:
            block += _row(p["after"], i)
        if p["before"]:
            block += ["Before, at the base commit:", ""] + _row(p["before"], i + " before")
        if p["red"]:
            block += ["What the verifier saw:", "", _img(p["red"], i + " red"), ""]
        if fold:
            lines += ["<details>", f"<summary>{head}</summary>", ""] + block + ["</details>", ""]
        else:
            lines += [f"**{head}**", ""] + block
    return lines
