"""hb.ledger - the delivery ledger: one row per contract item, and what makes a row valid."""

import json
import os
import re
import time
import uuid
from pathlib import Path

from hb.rig.gh import _anchor_goal_ids
from hb.contract.manifest import _manifest_items, _manifest_read
from hb.rig.common import _ledger_path, _ledger_read, canon, cfg, die, doc_root, now_iso, out, state_dir


_LEDGER_STATES = ("WORKS", "BROKEN", "UNMEASURED", "DIVERGED")
_LEDGER_SUBSTATES = {
    "WORKS": (),                       # §0: WORKS carries no substate
    "BROKEN": ("failed", "mismatch", "not-built", "infeasible"),
    # the battery's two: no runner or no case set named (`no-battery`), and a runner that ran
    # and could not be read (`battery-runner`); each names why the number is missing (Q4)
    "UNMEASURED": ("not-driven", "verifier-failed", "stack-failed", "no-battery", "battery-runner"),
    # an item built ON PURPOSE to something other than its then-clause (a standing
    # ruling or the ruled mock contradicted the manifest sentence). Not a defect
    # and not a gap: a decision for the owner, so the
    # row carries the docket id that asks for it and is counted apart from red.
    "DIVERGED": (),
}


def _ledger_line_id(line):
    """The event id a raw ledger line carries, for naming rows in a refusal."""
    try:
        o = json.loads(line.decode("utf-8"))
        return str(o.get("event_id") or o.get("candidate_id") or "?")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return "<unparseable line>"


def _ledger_compose(prior, rows):
    """The delivery ledger's NEXT content: the prior bytes VERBATIM, plus one JSON
    line per new row.

    It is its own function so the append-only property has something to be checked
    against, and so a mutation can prove the guard fires rather than assert it.
    """
    body = "".join(json.dumps(ev, ensure_ascii=False) + "\n"
                   for ev in rows).encode("utf-8")
    if prior and not prior.endswith(b"\n"):
        prior = prior + b"\n"
    return prior + body


def _ledger_append_guard(prior, nxt):
    """None when `nxt` preserves every row already on disk, else a NAMED refusal
    dict.

    The test is the PREFIX property. The ledger is append-only by contract, so any
    deletion line on it is a defect by construction, and a reordering is a deletion
    wearing a rewrite. The count and the ids that would be lost are computed too,
    because "rows would be lost" and "which rows" route differently for whoever
    has to repair it.
    """
    if nxt.startswith(prior):
        return None
    old_lines = [ln for ln in prior.splitlines() if ln.strip()]
    kept = set(ln for ln in nxt.splitlines() if ln.strip())
    lost = [ln for ln in old_lines if ln not in kept]
    return {"why": f"{len(old_lines)} row(s) were on disk and the composed content "
                   "is not a prefix-preserving extension of them; "
                   f"{len(lost)} of those row(s) appear nowhere in it.",
            "prior_rows": len(old_lines), "would_lose": len(lost),
            "lost_row_ids": [_ledger_line_id(ln) for ln in lost[:10]]}


def _receipts_path(c, issue):
    """v9: ONE receipts file per slice. The candidate-directory layout is gone:
    it split a slice's evidence across two trees (a receipt here, a candidate
    there) that could disagree, and a host-global candidates dir let one slice's
    fold sweep a sibling's pending rows."""
    return state_dir(c) / "evidence" / str(issue).lstrip("#") / "receipts.jsonl"


# The served sha is the venue record's `served_sha`, which the venue's own receipt gave;
# the caller passes it with --served-sha. The engine never probes for it and never guesses it.
SERVED_SHA_NOT_GIVEN = "not given"


def _viewport_widths(c):
    """The widths of config `pictures.viewports`, joined, or None when the product names none."""
    viewports = (c.get("pictures") or {}).get("viewports") or []
    widths = [str(v.get("width")) for v in viewports if isinstance(v, dict) and v.get("width")]
    return ",".join(widths) or None


# The item kinds whose drive reads a venue: a row for one always names the tenant it read.
VENUE_DRIVEN_KINDS = ("screen", "form", "list", "chat-verb", "api")


def _ledger_state_check(state, substate):
    if state not in _LEDGER_STATES:
        return (f"state {state!r} is not one of {_LEDGER_STATES}: no other state, "
                "no blank rows.")
    allowed = _LEDGER_SUBSTATES[state]
    if state in ("WORKS", "DIVERGED"):
        if substate:
            return (f"{state} carries no substate (got {substate!r}); a DIVERGED row "
                    "carries the ruling it waits on instead")
        return None
    if not substate:
        return (f"{state} requires a substate, one of {allowed}. §0 codex-hardening: "
                "each top state carries a NAMED substate because the decisions they "
                "demand differ: a bare red row tells the owner nothing to decide.")
    if substate not in allowed:
        return f"substate {substate!r} is not one of {allowed} for state {state}"
    return None


def cmd_ledger(a):
    c = cfg()
    # receipts, folds and reads all resolve against the INVOKING
    # worktree. A fold that writes canon lands its rows on a branch nobody is on.
    root = doc_root()

    # ── receipt ─────────────────────────────────────────────────────────────
    if a.op == "receipt":
        declared_out = (getattr(a, "declared_out", None) or "").strip() or None
        # the ledger has a STATE for never-driven items and this is its FILING
        # PATH: demanding drive metadata (tenant, evidence) for an absence
        # forces the filer to fabricate a tenant identity
        # to make coverage read complete (the conductor nearly did, for
        # cosmetics, and the gate caught it). --declared-out files
        # UNMEASURED/not-driven WITH THE REASON and WITHOUT attribution,
        # because there is nothing to attribute.
        if declared_out:
            for req in ("issue", "item", "sha"):
                if not getattr(a, req, None):
                    die(f"ledger receipt --declared-out requires --{req}")
            if a.state and a.state.upper() != "UNMEASURED":
                out({"error": "ledger receipt: --declared-out files "
                              "UNMEASURED/not-driven by definition: a declared-out "
                              "item with any other state is a contradiction.",
                     "verdict": "declared-out-state-conflict"}, 2)
        else:
            for req in ("issue", "item", "sha", "state"):
                if not getattr(a, req, None):
                    die(f"ledger receipt requires --{req}")
        issue = a.issue.lstrip("#")
        state = "UNMEASURED" if declared_out else a.state.upper()
        sub = "not-driven" if declared_out else ((a.substate or "").strip() or None)
        bad = _ledger_state_check(state, sub)
        if bad:
            out({"error": f"ledger receipt: {bad}", "verdict": "bad-state",
                 "item": a.item}, 2)
        if state == "DIVERGED" and not (getattr(a, "ruling", None) or "").strip():
            out({"error": "ledger receipt: a DIVERGED row must carry --ruling <dk-id>, "
                          "the docket line that asks the owner to rule between the "
                          "manifest's sentence and what was built. A divergence with no "
                          "question attached is just an unexplained miss.",
                 "verdict": "diverged-without-ruling", "item": a.item}, 2)
        if state == "WORKS" and not a.evidence:
            out({"error": "ledger receipt: a WORKS row must carry at least one "
                          "--evidence path. §0: WORKS means 'driven live and "
                          "matched the manifest/artifact', and a claim with no "
                          "artifact is unciteable.",
                 "verdict": "works-without-evidence", "item": a.item}, 2)
        # A WORKS or BROKEN row states the SITUATION the verifier built before
        # driving (the fixture recipe made real: which venue state, what
        # was placed, which screen it started from). Every false red of one
        # whole wave was a route the verifier invented; a row that cannot say what it built
        # can only be untested. Design-source rows re-read a capture and build nothing.
        _situation = (getattr(a, "situation", None) or "").strip()
        if state in ("WORKS", "BROKEN", "DIVERGED") and not _situation \
                and (getattr(a, "source", None) or "slice") != "design":
            out({"error": "ledger receipt: a WORKS or BROKEN row needs --situation "
                          "\"<what was built before the drive: the venue state, the rows placed, "
                          "the screen it started on>\". Without it the row is UNMEASURED/"
                          "not-driven by rule, never a verdict.",
                 "verdict": "no-situation", "item": a.item}, 2)
        # v9.1 (N): the docs call these paths EVIDENCE_DIR-relative, so resolve them
        # that way BEFORE asking whether they exist. A dry run once wrote correct
        # relative paths and every receipt died on `evidence-missing`, because the
        # check resolved them against the caller's cwd instead.
        _evdir = _receipts_path(c, issue).parent
        _ev_resolved, missing = [], []
        for _p in (a.evidence or []):
            _cand = Path(_p)
            if not _cand.is_absolute() and not _cand.exists():
                _cand = _evdir / _p
            if _cand.exists():
                _ev_resolved.append(str(_cand.resolve()))
            else:
                missing.append(f"{_p} (also tried {_evdir / _p})")
        if missing:
            out({"error": "ledger receipt: cited evidence path(s) do NOT exist on "
                          f"disk: {missing}. A cited-but-absent receipt is "
                          "UNMEASURED, never a pass: write the "
                          "artifact first, then the row.",
                 "verdict": "evidence-missing", "missing": missing}, 2)
        # Verify the item exists in the manifest AT THE DECLARED REV. A row for an
        # item@rev the manifest does not carry cannot be joined to anything.
        slug = a.slug or a.wave
        rev = a.rev
        manifest_state = "absent"
        doc = None
        if slug:
            doc, merr = _manifest_read(slug, root)
            if merr and root != canon():
                # composed receipts written from the wave
                # worktree read no manifest (the tree lacked it) and landed at rev 0.
                # The canon copy is the fallback, and the row names which answered.
                _doc2, _merr2 = _manifest_read(slug, canon())
                if not _merr2:
                    doc, merr = _doc2, None
            if merr:
                # v9.1 (N): a missing or unusable manifest no longer refuses. The
                # row is filed with `manifest: "absent"` and --item is taken as the
                # caller gave it. A slice with no manifest still delivers things,
                # and refusing its receipts left the delivery unrecorded entirely.
                doc = None
        # A GOAL row is checked against the ANCHOR, never the manifest: the wave's
        # goals live in the anchor's `## GOAL` block and no manifest item carries
        # their ids. Without this door the only contract items a wave owns as a
        # whole could never be filed as rows, and they were written as prose.
        is_goal = bool(re.fullmatch(r"G\d+", a.item or ""))
        if is_goal:
            anchor_arg = (getattr(a, "anchor", None) or "").strip()
            if not anchor_arg:
                out({"error": f"ledger receipt: {a.item!r} is a GOAL id, and a wave's "
                              "goals are declared on its ANCHOR, not in the manifest. "
                              "Pass --anchor <issue> so the id is checked against what "
                              "the wave set out to do.",
                     "verdict": "goal-without-anchor", "item": a.item}, 2)
            goal_ids, goal_why = _anchor_goal_ids(c, anchor_arg)
            if a.item not in goal_ids:
                out({"error": f"ledger receipt: {a.item!r} is not declared in the "
                              f"`## GOAL` block of anchor "
                              f"#{anchor_arg.lstrip('#')}"
                              + (f" ({goal_why})" if goal_why else "")
                              + ". A goal row for an id the wave never set joins to "
                                "nothing and would read as extra delivery.",
                     "verdict": "unknown-goal", "known_ids": goal_ids}, 2)
            if doc is not None:
                manifest_state = doc["_path"]
            if rev is None:
                # The goal is the wave's, not one item's, so it is bound to the
                # manifest as a whole: its top-level rev, or 0 where none is read.
                try:
                    rev = int(doc.get("rev")) if doc is not None else 0
                except (TypeError, ValueError):
                    rev = 0
        elif doc is not None:
            manifest_state = doc["_path"]
            hit = [it for it in _manifest_items(doc)
                   if isinstance(it, dict) and str(it.get("id")) == a.item]
            if not hit:
                out({"error": f"ledger receipt: item {a.item!r} is not in manifest "
                              f"{doc['_path']}. The ledger's rows ARE the manifest's "
                              "items (§0): a row for an unknown id joins to nothing "
                              "and would read as extra delivery.",
                     "verdict": "unknown-item",
                     "known_ids": [str(i.get("id")) for i in _manifest_items(doc)
                                   if isinstance(i, dict)][:40]}, 2)
            mrev = hit[0].get("rev")
            if rev is None:
                rev = mrev
            elif rev != mrev:
                out({"error": f"ledger receipt: --rev {rev} but manifest "
                              f"{doc['_path']} carries {a.item}@rev{mrev}. A row is "
                              "bound to item@rev; a mismatch is REFUSED rather than "
                              "coerced, because silently filing against the wrong "
                              "rev makes an amended item look delivered.",
                     "verdict": "rev-mismatch"}, 2)
        elif rev is None:
            # v9.1 (N): with no manifest there is no rev to read, and demanding one
            # would just be a number invented to satisfy a join that does not exist.
            rev = 0
        # An UNMEASURED row read no substance, so it names no tenant; requiring one there
        # refused every stack-failed row of a run and lost the record.
        # A measure item, or a row the verifier read from the tree or a unit pin
        # (--no-venue), read no venue either; a venue-driven kind always names its tenant.
        item_kind = str((hit[0].get("kind") if not is_goal and doc is not None else "") or "").lower()
        tenant_id = (getattr(a, "tenant_id", None) or "").strip() or None
        no_venue = (getattr(a, "no_venue", None) or "").strip() or None
        if item_kind == "measure" and not no_venue:
            no_venue = "a measure item reads no venue"
        venue_free = bool(no_venue) and item_kind not in VENUE_DRIVEN_KINDS
        if not tenant_id and not declared_out and state != "UNMEASURED" and not venue_free:
            out({"error": "ledger receipt: --tenant-id is required"
                          + (f" for a `{item_kind}` item" if item_kind else "")
                          + ". §1.5 pairs every "
                          "UI drive with a SUBSTANCE read (the DB row or API "
                          "response behind it), and a substance read that does not "
                          "name which tenant it read is not attributable to anything. "
                          "This is refused rather than defaulted: a guessed tenant is "
                          "the fill-value defect (a fill value is indistinguishable "
                          "from a measured one). A row read from the tree or a unit "
                          "pin on a kind that drives no venue passes --no-venue <why>.",
                 "verdict": "no-tenant-identity", "kind": item_kind or None}, 2)
        tenant_basis = "measured" if tenant_id else ("no-venue" if venue_free else None)
        served_sha = (getattr(a, "served_sha", None) or "").strip() or SERVED_SHA_NOT_GIVEN
        receipt = {
            "schema": "drive-evidence/v1",
            "receipt_id": uuid.uuid4().hex,
            "written_by": "hb ledger receipt",     # §1.5: a MACHINE receipt, so
            "written_ts": now_iso(),               # 'evidence exists' and 'the gate
            "item": a.item, "rev": rev,            # ran' are the same fact
            "state": state, "substate": sub,
            # a wave-less slice books under `standalone`, so the `next` hint below runs
            "sha": a.sha, "issue": f"#{issue}",
            "wave": a.wave or "standalone",
            "slug": slug,
            "observed": (a.observed or None) if not declared_out else
                        f"DECLARED OUT-OF-WAVE, never driven: {declared_out}",
            "situation": _situation or None,
            "ruling": (getattr(a, "ruling", None) or None),
            "plan": (getattr(a, "plan", None) or None),
            "venue": (getattr(a, "venue", None) or None),
            "evidence": _ev_resolved,
            "manifest": manifest_state,
            "widths": a.widths or _viewport_widths(c),
            "env_identity": {
                "tenant_id": tenant_id,
                "tenant_basis": tenant_basis,
                "no_venue": no_venue if venue_free and not tenant_id else None,
                "served_sha": served_sha,
                "status": "not-applicable: declared-out, no drive happened, "
                          "nothing to attribute" if declared_out else
                          "measured" if served_sha != SERVED_SHA_NOT_GIVEN else
                          "PARTIAL: the venue record gave no served_sha, so which "
                          "build was driven is UNMEASURED. The drive still happened "
                          "against the tenant named above; what is unknown is the "
                          "code identity behind it.",
            },
        }
        # v9: --source says whether this row came from the SLICE's own drive or
        # from the composed wave drive. `append` resolves a composed row over a
        # slice row for the same item, which is the only precedence rule left.
        receipt["source"] = (getattr(a, "source", None) or "slice")
        if is_goal:
            receipt["kind"] = "goal"
            receipt["anchor"] = str(anchor_arg).lstrip("#")
        rp = _receipts_path(c, issue)
        rp.parent.mkdir(parents=True, exist_ok=True)
        # ONE file per slice, appended a line at a time. Append mode plus a single
        # line per write is atomic enough for the concurrency this actually sees
        # (one writer per slice), and a torn line is visible as a parse error at
        # append time rather than a silently missing row.
        with rp.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt, ensure_ascii=False) + "\n")
        out({"verdict": "receipt-written", "receipts": str(rp),
             "receipt_id": receipt["receipt_id"], "source": receipt["source"],
             "item": a.item, "rev": rev, "state": state, "substate": sub,
             "env_identity_status": receipt["env_identity"]["status"],
             "next": f"`hb ledger append --wave {receipt['wave']}` resolves the "
                     "wave's receipts and appends the live rows to the tracked "
                     "ledger (single writer)."}, 0)

    # -- append ---------------------------------------------------------------
    if a.op == "append":
        # SINGLE WRITER by construction: one process, one lock, one append pass.
        # Slices write their own receipts files concurrently; this is the only
        # thing that touches the cumulative ledger.
        if not a.wave:
            out({"error": "ledger append: --wave <slug> is required - it names "
                          "whose receipts to resolve.",
                 "verdict": "append-unscoped"}, 2)
        # The one source of which rows to fold: the receipts files of the issues the runner
        # names with --issues (each slice's own), and the anchor's (a receipts file
        # on disk is the record).
        only = sorted({x.strip().lstrip("#") for x in str(getattr(a, "issues", None) or "").split(",") if x.strip()})
        miss = [i for i in only if not _receipts_path(c, i).exists()]
        if miss:
            out({"error": f"ledger append: --issues names {miss} and no receipts file exists for them.",
                 "verdict": "issues-not-in-wave"}, 2)
        slices = [(i, {"wave": a.wave, "issue": f"#{i}"}) for i in only]
        # a composed drive files its GOAL and cross-slice rows on the ANCHOR;
        # named with --anchor, its receipts file is read too.
        _anchor = (getattr(a, "anchor", None) or "").lstrip("#")
        if _anchor and _anchor not in {i for i, _ in slices} and _receipts_path(c, _anchor).exists():
            slices.append((_anchor, {"wave": a.wave, "issue": f"#{_anchor}",
                                     "_attribution": "anchor receipts (composed drive)"}))
        # Read every receipt line for those slices.
        rows, unreadable = [], []
        for issue, _d in slices:
            rp = _receipts_path(c, issue)
            if not rp.exists():
                continue
            for n_, line in enumerate(rp.read_text(encoding="utf-8").splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append((issue, json.loads(line)))
                except json.JSONDecodeError as ex:
                    unreadable.append(f"{rp}:{n_}: {ex}")
        if unreadable:
            out({"error": "ledger append: receipt line(s) are UNREADABLE. NOTHING "
                          "was appended - a partial append reports a smaller ledger "
                          "with no sign anything was dropped.",
                 "verdict": "receipts-unreadable", "unreadable": unreadable[:10]}, 2)

        # -- resolve the LIVE row per item id ---------------------------------
        # A `composed` row beats a `slice` row (the composed drive is the later,
        # wider measurement of the same item); otherwise the latest timestamp
        # wins. That is the whole precedence rule in v9: no explicit supersession
        # fields, no SHA-descent arithmetic, no sibling groups.
        def _rank(r):
            return (1 if (r.get("source") or "slice") == "composed" else 0,
                    str(r.get("written_ts") or ""))
        live = {}
        for issue, r in rows:
            key = str(r.get("item"))
            if key not in live or _rank(r) >= _rank(live[key][1]):
                live[key] = (issue, r)

        # -- the only two refusals --------------------------------------------
        problems = []
        # every cited evidence path must be on disk
        for key, (issue, r) in sorted(live.items()):
            for evp in (r.get("evidence") or []):
                # from the wave worktree every evidence path read
                # 'not on disk' because a relative path was resolved against the cwd.
                # Harness evidence lives under canon; canon is the second door.
                if not Path(evp).exists() and not (canon() / evp).exists():
                    problems.append(f"#{issue} {key}: evidence {evp} is not on disk")
        # every item id must be in the manifest
        # v9.1 (N): an item id is checked against a manifest ONLY when one exists.
        # No manifest means no id list to be unknown against, so the row is filed as
        # given; the receipt already records `manifest: "absent"` so the absence is
        # on the row rather than inferred later.
        manifests, unknown, unchecked = {}, [], []
        for key, (issue, r) in sorted(live.items()):
            slug = r.get("slug")
            if r.get("kind") == "goal":
                # A goal row was validated against the anchor's GOAL block when it
                # was filed; no manifest carries its id, so checking it against one
                # would refuse every goal the wave declared.
                continue
            if not slug:
                unchecked.append(f"#{issue} {key}: no manifest slug on the receipt")
                continue
            if slug not in manifests:
                doc_, merr_ = _manifest_read(slug, root)
                if merr_ and root != canon():
                    _d2, _m2 = _manifest_read(slug, canon())
                    if not _m2:
                        doc_, merr_ = _d2, None
                manifests[slug] = (None if merr_ else doc_, merr_)
            doc_, merr_ = manifests[slug]
            if merr_:
                unchecked.append(f"#{issue} {key}: manifest {slug!r} not usable "
                                 f"({merr_['verdict']})")
                continue
            ids = {str(i.get("id")) for i in _manifest_items(doc_)
                   if isinstance(i, dict)}
            if key not in ids:
                unknown.append(f"#{issue} {key}: not an item id in {doc_['_path']}")
        problems.extend(unknown)
        if problems:
            out({"error": "ledger append: the resolved rows do not satisfy the row "
                          "contract. NOTHING was appended - an append is "
                          "all-or-nothing so a half-written ledger cannot be "
                          "mistaken for a complete one.",
                 "verdict": "rows-refused", "problems": problems[:20],
                 "resolved": len(live)}, 2)

        events, lerr = _ledger_read(root)
        if lerr and lerr.get("verdict") == "ledger-damaged":
            out({**lerr, "error": lerr["error"] + " The append refuses to write to "
                                                  "a damaged ledger."}, 2)
        seen = {e.get("receipt_id") for e in events}
        new_events = []
        for key, (issue, r) in sorted(live.items()):
            if r.get("receipt_id") in seen:
                continue
            new_events.append({
                "schema": "ledger-event/v1", "event_id": uuid.uuid4().hex,
                "ts": now_iso(), "appended_by": "hb ledger append",
                "receipt_id": r.get("receipt_id"),
                "item": r.get("item"), "rev": r.get("rev"),
                "state": r.get("state"), "substate": r.get("substate"),
                "sha": r.get("sha"), "issue": r.get("issue") or f"#{issue}",
                "wave": r.get("wave"), "slug": r.get("slug"),
                "source": r.get("source") or "slice",
                "observed": r.get("observed"),
                "evidence": r.get("evidence") or [],
                "env_identity": r.get("env_identity"),
                "driven_ts": r.get("written_ts"),
                **({"kind": "goal", "anchor": r.get("anchor")}
                   if r.get("kind") == "goal" else {})})

        lp_ = _ledger_path(root)
        lp_.parent.mkdir(parents=True, exist_ok=True)
        lock = lp_.with_suffix(".lock")
        for _ in range(50):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                time.sleep(0.2)
        else:
            die("delivery ledger lock held >10s - another append is running (the "
                "ledger has ONE writer by design); remove the .lock if orphaned", 2)
        try:
            # APPEND-ONLY BY CONSTRUCTION. Losing a row must be
            # IMPOSSIBLE, not merely detectable: the next content is composed from
            # the prior bytes verbatim, the prefix property is checked BEFORE
            # anything is written, and the file is re-read afterwards and RESTORED
            # from the prior bytes if the property does not hold on disk.
            prior_bytes = lp_.read_bytes() if lp_.exists() else b""
            nxt = _ledger_compose(prior_bytes, new_events)
            gerr = _ledger_append_guard(prior_bytes, nxt)
            if gerr:
                out({"error": "ledger append: the composed content would LOSE rows "
                              "already on disk. NOTHING was written. " + gerr["why"],
                     "verdict": "append-refused-would-drop-rows",
                     "ledger": str(lp_), **gerr}, 2)
            tmp_ = Path(str(lp_) + ".tmp")
            tmp_.write_bytes(nxt)
            os.replace(tmp_, lp_)
            verr = _ledger_append_guard(prior_bytes, lp_.read_bytes())
            if verr:
                tmp_.write_bytes(prior_bytes)
                os.replace(tmp_, lp_)
                out({"error": "ledger append: the ledger ON DISK after the write no "
                              "longer carries every row it carried before. The prior "
                              "bytes were RESTORED and the append is refused. "
                              + verr["why"],
                     "verdict": "append-post-write-drop-restored",
                     "ledger": str(lp_), **verr}, 2)
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass
        out({"verdict": "appended", "appended": len(new_events),
             "event_ids": [e["event_id"] for e in new_events],
             "resolved_rows": len(live), "receipts_read": len(rows),
             "item_ids_unchecked": unchecked,
             "slices": sorted({i for i, _ in slices}),
             "already_present": len(live) - len(new_events),
             "ledger": str(lp_),
             "note": "the ledger is a TRACKED file - commit it with the wave that "
                     "produced these rows."}, 0)

    die(f"unknown ledger op {a.op}")


