"""hb.run.policy - what the runner decides about rows and findings, and nothing else.

Every open finding and every red goes to the builder; there is no classification step. The
one exception is the owner's stop list (schemas.stop_kinds()): a `nonwaivable` finding stops the
run until the owner answers its id, and then it goes to the builder with the answer. A
DIVERGED row comes only from a ruling or a manifest revision, never from the builder's note.
"""
import hashlib
import json
import re
from pathlib import Path

from hb.gates import verdict

def route_findings(findings, answered=None, goal_met=False, scope=None):
    """(to the builder, stops, riding): a `nonwaivable` finding the owner has not answered by its id
    stops the run (stops.py); an answered one carries its answer to the builder. Once the review says
    the goal is met, a finding below P1 rides the record for the owner instead of relaunching the
    builder (ruling 18), and so does any finding whose quote falls outside `scope`, the paths the
    last round touched, because a fresh reading of a large diff always has something new to say."""
    fix, stop, ride = [], [], []
    for f in findings or []:
        if f.get("status") == "fixed":
            continue
        answer = (answered or {}).get(str(f.get("id") or "").casefold())
        if answer:
            f = dict(f, answered=True, answer=answer)
        if f.get("class") == "nonwaivable" and not answer:
            stop.append(f)
            continue
        quoted = ((f.get("quote") or {}).get("file") or "").replace("\\", "/")
        p1 = str(f.get("severity") or "P1").upper() == "P1"
        if scope is not None and quoted and quoted not in scope and not p1 and not answer:
            ride.append(dict(f, rides="outside the paths the round touched"))
        elif goal_met and not p1 and not answer:
            ride.append(dict(f, rides="below P1 after the goal was met"))
        else:
            fix.append(f)
    return fix, stop, ride


def goal_met(report):
    """The last review's word on the goal; a report with no verdict has not said it was met."""
    return bool(((report or {}).get("goal_verdict") or {}).get("met"))


def relabel_diverged(receipts, note, rulings_text):
    """A BROKEN row becomes DIVERGED only when a ruling the run carries names the
    item as built to something other than the manifest sentence. The builder's
    note may explain a red; it may never relabel one."""
    ids_in_rulings = set(re.findall(r"\b(dk-\d+|R\d+|ST-\d+|KEEP-\d+)\b", rulings_text or ""))
    by_item = {}
    for d in (note or {}).get("decisions") or []:
        rid = d.get("ruling")
        if rid and rid in ids_in_rulings:
            for it in (note or {}).get("items") or []:
                if it.get("status") == "built-differently" and it.get("item") \
                        and it["item"] in (d.get("text") or ""):
                    by_item[it["item"]] = rid
    out = []
    for r in receipts or []:
        r = dict(r)
        if r.get("state") == "BROKEN" and r.get("substate") == "mismatch" and r.get("item") in by_item:
            r["state"], r["substate"], r["ruling"] = "DIVERGED", None, by_item[r["item"]]
        out.append(r)
    return out


def note_beside(receipts, note):
    """Attach the builder's `because` to a red row so the owner reads the reason
    beside the red, without the label changing."""
    because = {it.get("item"): it.get("because") for it in (note or {}).get("items") or []
               if it.get("status") == "built-differently"}
    out = []
    for r in receipts or []:
        r = dict(r)
        if r.get("state") == "BROKEN" and r.get("item") in because:
            r["builder_note"] = because[r["item"]]
        out.append(r)
    return out


# ── limits ───────────────────────────────────────────────────────────────
# Two kinds of clock, ruled 2026-09-22. A STALL is the only cut on a working seat: no
# commit, no edit, no progress line and no output for this long means the seat is lost,
# and it is relaunched from its progress file. The FUSE is not a guardrail; it is the
# one hard stop on a runaway process. Tool ceilings below are fuses too, each set at
# several times the measured run so far.
SEAT_FUSE_MIN = 360          # measured seats: 1.4 to 52 minutes
CODEX_READ_MIN = 15          # measured 1.4 to 6 minutes
GATE_MIN = 60                # the suite measured 4 to 13 minutes
REPLAY_MIN = 30              # one shard of the suite at the base commit
VENUE_UP_MIN = 30            # measured 3 minutes, Docker cold start adds up to 6
VENUE_DOWN_MIN = 10
TEST_DB_MIN = 10             # the template build measured 1 to 4 minutes

# The drive's model (ruled 2026-09-22): the fake answers the same way every time, so it
# is the sharper instrument for mechanics; an item where the model itself decides can
# only be measured on the real model. A contract carrying one such item drives real, and
# so does one whose fixture recipe sends text through a model (eight screen
# items once rode the reader, drove on the fake, and measured nothing).
MODEL_JUDGMENT_KINDS = ("chat-verb", "chat", "model", "conversation")


def real_model_reason(items, changed_paths=()):
    by_kind = [it["item"] for it in items or [] if str(it.get("kind") or "").lower() in MODEL_JUDGMENT_KINDS]
    by_recipe = [it["item"] for it in items or [] if str(it.get("model") or "").lower() == "real"]
    by_path = model_paths(changed_paths)
    parts = []
    if by_kind:
        parts.append(f"a model-judgment item in the contract ({', '.join(by_kind[:4])})")
    if by_recipe:
        parts.append(f"a fixture recipe that runs the model ({', '.join(by_recipe[:4])})")
    if by_path:
        parts.append(f"a change to the model's own code ({', '.join(by_path[:4])})")
    return "; ".join(parts)


# Where a look can change and where the model itself decides: an item of neither kind is
# server behaviour, and its proof is the builder's test under the gates.
SCREEN_KINDS = ("screen", "flow", "ui")


def has_screen(items):
    return any(str(it.get("kind") or "").lower() in SCREEN_KINDS for it in items or [])


def needs_real_model(items):
    return any(str(it.get("kind") or "").lower() in MODEL_JUDGMENT_KINDS
               or str(it.get("model") or "").lower() == "real" for it in items or [])


def model_prefixes():
    """Where the product decides what its model says and does (config `model.paths`): a change
    there is judged only on the real model, whatever kind its items carry. Empty when the
    product names none."""
    from hb.rig.common import cfg
    return tuple(str(p) for p in ((cfg().get("model") or {}).get("paths") or []))


def model_paths(changed_paths):
    """The changed paths under the product's model code. A path is read repo-relative; an
    absolute one matches by the repo-relative part inside it."""
    prefixes = model_prefixes()
    if not prefixes:
        return []
    paths = (str(p).replace("\\", "/") for p in changed_paths or [])
    return sorted(p for p in paths if p.startswith(prefixes) or any("/" + m in p for m in prefixes))


def needs_real_model_for(items, changed_paths):
    """The items ask for the real model, or the change touches the model's own code."""
    return needs_real_model(items) or bool(model_paths(changed_paths))


def grey_at_filing(item):
    """An item the manifest filed as not drivable stays UNMEASURED by nature; the owner
    ruled it never blocks a merge."""
    return item.get("drivable") is False


def pinning_test(item, note):
    """The test in the builder's note that pins this item by its id, or None."""
    for t in (note or {}).get("tests") or []:
        if isinstance(t, dict) and re.search(rf"\b{re.escape(str(item.get('item')))}\b", str(t.get("pins") or "")):
            return t.get("path") or "a test"
    return None


def proven_by_test(items, note, tree=None):
    """One WORKS row per item whose proof is a test the builder's note names and, given the
    run's `tree`, that is a file in it: a test nobody wrote proves nothing. An item filed
    for a test with none named is driven like any other, so nothing goes unproven quietly."""
    rows = []
    for it in items or []:
        path = pinning_test(it, note) if it.get("proof") == "test" else None
        if path and tree is not None:
            # the ledger cites a file that exists, so the row carries the test's own path
            found = Path(tree) / str(path).split("::")[0]
            path = str(found) if found.is_file() else None
        if path:
            # the ledger takes a WORKS row with no substate; `by` says who proved it
            rows.append({"item": it["item"], "state": "WORKS", "substate": None, "by": "test",
                         "observed": f"proven by the builder's test {path}; the gates ran it",
                         "seen": f"pinned by {path}", "evidence": [path],
                         "situation": "no stand-in: the run's gates ran the test at this tip",
                         "no_venue": "proven by a test the gates ran; no venue was driven"})
    return rows


def regrade_grey_by_test(receipts, items, note, tree=None):
    """A BROKEN row on an item the manifest filed grey, re-graded by the test the builder's
    note names for it: a grey item is never driven again, so a red no drive can clear would
    block the merge for good. A grey item with no such test keeps its row."""
    grey = {it["item"]: it for it in items or [] if grey_at_filing(it)}
    broken = [dict(grey[r["item"]], proof="test") for r in receipts or []
              if r.get("state") == "BROKEN" and r.get("item") in grey]
    proven = {r["item"]: r for r in proven_by_test(broken, note, tree)}
    return [proven.get(r.get("item"), r) if r.get("state") == "BROKEN" else r for r in receipts or []]


def owed_unmeasured(receipts, items):
    """The rows a `--repair` drives again with no fixer: every UNMEASURED row on an item the
    manifest did not file grey, and every such item no row speaks for. Nothing is known to be
    wrong with them, and a repair that called them nothing to do left them unmeasured for good."""
    grey = {it["item"] for it in items or [] if grey_at_filing(it)}
    have = {r.get("item") for r in receipts or []}
    owed = [r.get("item") for r in receipts or [] if r.get("state") == "UNMEASURED"
            and r.get("item") not in grey and r.get("by") != "test"]
    return owed + [it["item"] for it in items or [] if it["item"] not in have and it["item"] not in grey]


def blocking_red(receipts, items):
    """The rows that make a run ship red: BROKEN, and UNMEASURED on an item the manifest
    did not file grey."""
    grey = {it["item"] for it in items or [] if grey_at_filing(it)}
    return [r for r in receipts or []
            if r.get("state") == "BROKEN" or (r.get("state") == "UNMEASURED" and r.get("item") not in grey)]


def receipts_from_checks(rows, items):
    """One receipt per rule from its cases' check rows: WORKS when every case is WORKS,
    BROKEN naming the first red case and its line, UNMEASURED naming the first unmeasured
    case's substate. The checks speak per case; the ledger books per rule (Q1)."""
    by_rule = {}
    for r in rows or []:
        by_rule.setdefault(r.get("rule"), []).append(r)
    out = []
    for it in items or []:
        mine = by_rule.get(it["item"])
        if not mine:
            continue
        red = next((r for r in mine if r.get("state") == "BROKEN"), None)
        grey = next((r for r in mine if r.get("state") == "UNMEASURED"), None)
        if red:
            state, sub, said = "BROKEN", "failed", f"case {red['case']}: {red.get('line')}"
        elif grey:
            # the ledger knows three UNMEASURED substates; the check's own one is named in the words
            state, sub, said = "UNMEASURED", "not-driven", f"{grey.get('substate')}: case {grey['case']}: {grey.get('observed')}"
        else:
            state, sub, said = "WORKS", None, f"every case held: {', '.join(r['case'] for r in mine)}"
        boundaries = sorted({str(r.get("boundary")) for r in mine if r.get("boundary")})
        out.append({"item": it["item"], "state": state, "substate": sub, "observed": said, "seen": said,
                    "situation": f"the product's checks for {it['item']}: each case's setup, then its assert at the "
                                 f"{' and '.join(boundaries) or 'named'} boundary",
                    "by": "checks", "cases": {r["case"]: r.get("state") for r in mine},
                    "identity": mine[0].get("identity"), "check": mine[0].get("check")})
    return out


def _audit_case_states(receipt, case_ids):
    """{case: WORKS|BROKEN} as the audit saw them: its own per-case map when it wrote one,
    else every case WORKS on a WORKS row, and on a BROKEN row the case it names (every case
    when it names none, since one of them failed)."""
    if isinstance(receipt.get("cases"), dict):
        return {k: v for k, v in receipt["cases"].items() if k in case_ids}
    if receipt.get("state") == "WORKS":
        return dict.fromkeys(case_ids, "WORKS")
    if receipt.get("state") != "BROKEN":
        return {}
    words = f"{receipt.get('observed') or ''} {receipt.get('seen') or ''}"
    named = [cid for cid in case_ids if re.search(rf"(?<![\w-]){re.escape(cid)}(?![\w-])", words)]
    return dict.fromkeys(named or case_ids, "BROKEN")


def audit_disagreements(check_rows, audit_receipts):
    """The cases the blind audit and the builder's checks disagree on: `false_greens` (the
    check WORKS, the audit BROKEN) and `false_reds` (the check BROKEN, the audit WORKS),
    each {case, rule, check, audit}. The false greens are the honesty score (Q9)."""
    by_rule = {}
    for r in check_rows or []:
        by_rule.setdefault(r.get("rule"), {})[r.get("case")] = r
    out = {"false_greens": [], "false_reds": []}
    for rec in audit_receipts or []:
        cases = by_rule.get(rec.get("item")) or {}
        for cid, seen in _audit_case_states(rec, list(cases)).items():
            said = cases[cid].get("state")
            if said == "WORKS" and seen == "BROKEN":
                kind = "false_greens"
            elif said == "BROKEN" and seen == "WORKS":
                kind = "false_reds"
            else:
                continue
            out[kind].append({"case": cid, "rule": rec.get("item"), "check": cases[cid].get("line") or "WORKS",
                              "audit": rec.get("observed")})
    return out


def stamp_served(items, report, identity=None):
    """Each receipt carries the sha its own drive served, so a ledger row names the code
    that was measured, never the branch tip at landing, and the identity of the
    round that measured it (`identity`), so a later round compares like with like."""
    served = (report or {}).get("served_sha")
    return [dict(r, served_sha=r.get("served_sha") or served, **({"identity": identity} if identity else {}))
            for r in items or []]


# what a receipt is true of. No clock: every venue's process clock is UTC and the state doors
# move the venue's own clock
IDENTITY_KEYS = ("tip", "contract_hash", "harness", "venue")


def contract_hash(contract):
    text = json.dumps({k: v for k, v in (contract or {}).items() if k in ("goal", "items", "rulings", "goals")},
                      sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def identity(tip, contract, venue_facts, harness):
    """The identity a receipt is stamped with: the code tip, the contract, the harness version
    the journal says the run is on, and the venue's id as the venue record names it."""
    vf = venue_facts or {}
    return {"tip": tip, "contract_hash": contract_hash(contract), "harness": harness, "venue": vf.get("venue")}


def invalidate(receipts, items, why):
    """The receipts of `items` set UNMEASURED by `why`: an input they were true under changed,
    and a row measured under another input is not carried."""
    named = set(items or [])
    out = []
    for r in receipts or []:
        if r.get("item") in named:
            out.append({"item": r.get("item"), "state": "UNMEASURED", "substate": "invalidated", "observed": why,
                        "seen": why})
        else:
            out.append(r)
    return out


def declared_files(item):
    """The repo paths an item declares, as git prints them: a `path:line` entry is its path."""
    names = item.get("files") or []
    if isinstance(names, str):
        names = [names]
    return {str(f).replace("\\", "/").split(":")[0].strip() for f in names if str(f).strip()}


def rows_declaring(items, touched):
    """The items that declare a file in `touched`: their rows were measured on code that has
    since moved. An item that declares no touched file keeps its row."""
    touched = {str(t).replace("\\", "/") for t in touched or []}
    return [it["item"] for it in items or [] if not grey_at_filing(it) and touched & declared_files(it)]


def merge_redrive(receipts, redriven, asked):
    """The rows a re-drive replaces: every row it was asked for, and any other row it
    measured (WORKS or BROKEN). A row it was not asked for is never erased by a re-drive
    that could not drive it (a stack-failed second drive once read every green as
    unmeasured), and a row it did not report keeps the earlier measurement. Carried rows
    are marked so the ledger and the report say so. A valid BROKEN row is never downgraded
    by a rerun that did not execute (a crashed seat, a lost venue): it stays BROKEN and
    names the rerun that failed it."""
    again = {r.get("item"): r for r in redriven or []}
    out = []
    for r in receipts or []:
        new = again.get(r.get("item"))
        if new is not None and (r.get("item") in asked or new.get("state") in ("WORKS", "BROKEN")):
            was, now = verdict.classify(r), verdict.classify(new)
            if was["valid"] and was["outcome"] == verdict.BROKEN and not now["valid"]:
                out.append(dict(r, rerun_invalid=f"{now['signature']}: {new.get('observed') or new.get('substate')}"))
                continue
            out.append(new)
        else:
            kept = dict(r)
            if r.get("item") not in asked and r.get("state") == "WORKS":
                kept["carried"] = True
            out.append(kept)
    return out


def authority_stops(note):
    """The builder's `needs_authority` rows that stop the run: the owner's stop list."""
    from hb.runner.schemas import stop_kinds
    kinds = stop_kinds()
    return [na for na in (note or {}).get("needs_authority") or [] if na.get("kind") in kinds]
