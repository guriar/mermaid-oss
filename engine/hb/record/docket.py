"""hb.docket - the owner queue: causes in, verdicts out."""

import json
import os
import re

import time
from pathlib import Path

from hb.rig.common import _docket_load, _docket_path, _locked_rmw, atomic_write, die, now_iso, out


#
# canon() resolves via --git-common-dir, so even from a linked worktree these
# ops hit the host's review memory under the config's state_dir: a worktree's copy is a
# read-only cache that the next provisioning overwrites.


def _locked_write(p: Path, m: dict):
    """Lock-file + atomic write for the mutable JSON ledgers (review-memory, docket).
    OS-agnostic O_EXCL lockfile; a crashed writer's orphan lock is removed by hand."""
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = p.with_suffix(".lock")
    for _ in range(25):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            time.sleep(0.2)
    else:
        die(f"{p.name} lock held >5s: another writer is stuck; remove the .lock if orphaned", 2)
    try:
        atomic_write(p, json.dumps(m, ensure_ascii=False, indent=1))
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


# ── docket (v6.2 R1: THE owner queue) ──────────────────────────────────
#
# One host-level ledger for every owner-facing ask any wave or session produces.
# Items exit ONLY by owner verdict (approved/parked/discarded/noted); `parked`
# REQUIRES a revisit condition: a park without one is the old owed.jsonl graveyard.
# canon() resolves via --git-common-dir, so every worktree hits the same file.

_DOCKET_TYPES = ("RULE", "REVIEW", "CALL", "LIVE", "PROPOSAL", "BATCH")
_DOCKET_VERDICTS = ("approved", "parked", "discarded", "noted")
# docket/v3 (owner-ruled 2026-09-03): every item is born with a CAUSE BLOCK and a LANE.
# A symptom without a measured cause is not a docket item. The lane decides what the walk
# files: hotfix = one issue per cause for /hotfix; slice = seven-section slice body for
# /ship-slice; sitting = a topic for /design-sitting; ruling = owner-only (RULE, CALL).
# v9.3 (owner-ruled 2026-09-05): a fifth lane, RIG. Harness and tooling causes sat in
# the product lanes (a spawn-prep defect as hotfix/defect beside a card bug), so every
# walk re-sorted them by hand and the fixes rode an ad hoc in-session train. A rig item
# is walked FIRST after a wave closes, fixed in the session on the rig branch, batched on
# one train, and never becomes a GitHub issue.
_DOCKET_LANES = ("hotfix", "slice", "sitting", "ruling", "rig")
_CAUSE_CLASSES = ("defect", "missing-invariant", "wrong-seam", "environment")
_RULING_TYPES = ("RULE", "CALL")
_FILE_LINE_RE = re.compile(r"[\w./\\-]+\.(?:py|tsx?|jsx?|mjs|cjs|md|ya?ml|json|css|txt|sql):\d+")


def _cause_file_lines(cause):
    return {m.group(0).replace("\\", "/").lower() for m in _FILE_LINE_RE.finditer(cause or "")}


def _cause_block_lint(a):
    """Violations for the docket/v3 cause block on `add`. Empty list = clean."""
    v = []
    cause = (getattr(a, "cause", None) or "").strip()
    desired = (getattr(a, "desired", None) or "").strip()
    cls = (getattr(a, "cause_class", None) or "").strip()
    lane = (getattr(a, "lane", None) or "").strip()
    if not cause:
        v.append("--cause is REQUIRED: the MEASURED cause (a file:line, a probe result, a "
                 "driven behavior). What the owner sees goes in --plain; WHY it happens "
                 "goes here. A symptom without a cause is not a docket item.")
    if not desired:
        v.append("--desired is REQUIRED: the state that SHOULD exist, citing the ruling, "
                 "register line, manifest item, or design record it must satisfy. An item "
                 "with no desired state is a complaint, not a decision.")
    if cls not in _CAUSE_CLASSES:
        v.append(f"--cause-class must be one of {_CAUSE_CLASSES} (got {cls!r}): defect = "
                 "this line is wrong; missing-invariant = nothing forbade it; wrong-seam = "
                 "the logic lives in the wrong layer; environment = host or tooling.")
    if lane not in _DOCKET_LANES:
        v.append(f"--lane must be one of {_DOCKET_LANES} (got {lane!r}): hotfix = one "
                 "territory, no migration/dep/request-field/auth change, no mock needed; "
                 "slice = known shape but fails a hotfix gate; sitting = taste or a new "
                 "surface; ruling = owner-only judgment; rig = the harness or its tooling.")
    return v


def _docket_counts(ents):
    c = {}
    for e in ents:
        if e.get("status") == "open":
            c[e.get("type", "?")] = c.get(e.get("type", "?"), 0) + 1
    return c


def cmd_docket(a):
    m = _docket_load()
    ents = m["entries"]
    by_id = {e["id"]: e for e in ents}
    if a.op == "add":
        if a.type not in _DOCKET_TYPES:
            die(f"add requires --type in {_DOCKET_TYPES}")
        if not a.title:
            die("add requires --title")
        if not (a.plain or "").strip():
            out({"error": "docket: --plain is required: what the owner would notice, what happens "
                          "if we do nothing, what you recommend"}, 2)
        # docket/v3: RULE and CALL are owner-only and land in the ruling lane;
        # a lane given for them is overridden, never refused.
        if a.type in _RULING_TYPES:
            a.lane = "ruling"
        elif (getattr(a, "lane", None) or "") == "ruling":
            out({"error": f"lane 'ruling' is reserved for {_RULING_TYPES}; a {a.type} "
                          "item is work and takes hotfix, slice, or sitting"}, 2)
        cause_viol = _cause_block_lint(a)
        if cause_viol:
            out({"error": "docket/v3: cause block rejected: every item is born with "
                          "a measured cause, a desired state, a cause class, and a lane",
                 "violations": cause_viol}, 2)
        # v9.3: ONE cause, one item. Two fx slices filed the same spawn-prep defect and
        # the same absent guard file under two ids. A new cause that names a file:line
        # an OPEN item already names is refused with that id, unless --link names it
        # (then the new item records the link and files anyway, for a genuinely
        # different cause at the same line).
        mine = _cause_file_lines(getattr(a, "cause", ""))
        if mine:
            dupes = [e["id"] for e in ents if e.get("status") == "open"
                     and (mine & _cause_file_lines(e.get("cause", "")))]
            link = (getattr(a, "link", None) or "").strip()
            if dupes and link not in dupes:
                out({"error": "docket/v3: an OPEN item already names this "
                              f"file:line: {dupes}. Symptoms that share a cause are "
                              "ONE item; add your measurement to it with `hb docket "
                              "retype --id <id> --recurrence ...`, or pass "
                              "--link <id> if this is a different cause at the "
                              "same line.",
                     "verdict": "duplicate-cause", "open_items": dupes,
                     "file_lines": sorted(mine)}, 2)
        def dmut(dm):
            dents = dm["entries"]
            eid = f"dk-{(max((int(e['id'][3:]) for e in dents if e.get('id', '').startswith('dk-')), default=0) + 1):03d}"
            dm["schema"] = "docket/v3"
            entry = {"id": eid, "type": a.type, "title": a.title, "plain": a.plain or "",
                     "body": a.body or "",
                     "recommendation": a.rec or "", "wave": a.wave, "discovered_by": a.discovered_by,
                     "status": "open", "ts": now_iso()}
            entry["cause"] = (getattr(a, "cause", None) or "").strip()
            entry["desired"] = (getattr(a, "desired", None) or "").strip()
            entry["cause_class"] = getattr(a, "cause_class", None)
            entry["lane"] = getattr(a, "lane", None)
            if getattr(a, "recurrence", None):
                entry["recurrence"] = a.recurrence.strip()
            if getattr(a, "needs_ruling", False):
                # a WORK item that also needs the owner's word: it keeps its work
                # lane (a REVIEW cannot take `ruling`) and the walk shows the flag.
                entry["needs_ruling"] = True
            if getattr(a, "link", None):
                entry["links"] = [a.link.strip()]
            if a.legacy_id:
                entry["legacy_id"] = a.legacy_id
            if a.action_file:
                entry["prepared_action_file"] = a.action_file
            dents.append(entry)
            return {"added": eid, "type": a.type, "open_by_type": _docket_counts(dents)}, 0

        obj, code = _locked_rmw(_docket_path(), {"schema": "docket/v1", "entries": []}, dmut)
        out(obj, code)
    if a.op == "list":
        lane_f = getattr(a, "lane", None)
        sel = [e for e in ents if (not a.wave or e.get("wave") == a.wave)
               and (not a.status or e.get("status") == a.status)
               and (not lane_f or (e.get("lane") or ("ruling" if e.get("type") in _RULING_TYPES else "unlaned")) == lane_f)]
        out({"entries": sel, "count": len(sel), "open_by_type": _docket_counts(
            [e for e in ents if (not a.wave or e.get("wave") == a.wave)])})
    if a.op in ("verdict", "retype"):
        e = by_id.get(a.id or "")
        if not e:
            die(f"docket {a.op} requires a valid --id (got {a.id!r})")
        if a.op == "retype":
            changed = {}
            if a.type:
                if a.type not in _DOCKET_TYPES:
                    die(f"retype requires --type in {_DOCKET_TYPES}")
                e["type"] = a.type
                changed["type"] = a.type
            # docket/v3 backfill: retype also carries the cause block for legacy items.
            for k, argk in (("lane", "lane"), ("cause", "cause"), ("desired", "desired"),
                            ("cause_class", "cause_class"), ("recurrence", "recurrence")):
                val = getattr(a, argk, None)
                if val:
                    if k == "lane" and val not in _DOCKET_LANES:
                        die(f"--lane must be one of {_DOCKET_LANES}")
                    if k == "cause_class" and val not in _CAUSE_CLASSES:
                        die(f"--cause-class must be one of {_CAUSE_CLASSES}")
                    e[k] = val.strip() if isinstance(val, str) else val
                    changed[k] = e[k]
            if e.get("type") in _RULING_TYPES:
                e["lane"] = "ruling"
            if not changed:
                die("retype changed nothing: pass --type and/or --lane/--cause/--desired/--cause-class")
            _locked_write(_docket_path(), m)
            out({"id": e["id"], "changed": changed})
        if a.verdict not in _DOCKET_VERDICTS:
            die(f"verdict requires --verdict in {_DOCKET_VERDICTS}")
        if a.verdict == "parked" and not a.revisit:
            out({"error": "parked requires --revisit <condition>: a park without a revisit "
                          "condition is the old graveyard (R1)"}, 2)
        e["status"] = a.verdict
        e["ruled_ts"] = now_iso()
        if a.note:
            e["verdict_note"] = a.note
        if a.revisit:
            e["revisit"] = a.revisit
        _locked_write(_docket_path(), m)
        out({"id": e["id"], "status": e["status"],
             "open_remaining": sum(1 for x in ents if x.get("status") == "open")})
    die(f"unknown docket op {a.op}")
