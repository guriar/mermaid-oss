"""hb.run.record - the run's record, published immutable before the PR is written.

Every item's verdict with the identity it was measured under, the cause objects of each
fix pass, the rehearsal report, the rows whose loop ended by name, the pictures, the
retired tests with their named replacement, the gate lines and every seat's report go up
as one commit on `evidence/<run_id>` in the product repo, parented on the branch's previous
publish and never force-pushed, so the branch is append-only. The PR links that commit's
`record.md` by sha: what the owner reviewed never moves under the link. `record.json`
beside it lists every path the commit holds, and a prune keeps each of them.
"""
import hashlib
from hb.record.journal import RUN_COMPLETE
import json
import os
import re
import tempfile
from pathlib import Path

from hb.rig.common import report_text, run
from hb.rig.common import is_test_path
from hb.contract import battery
from hb.record import firstscreen
from hb.rig import gitops
from hb.record import pictures
from hb.record import success
from hb.venue import adapter
from hb.contract import policy
from hb.record.prbody import _answers_lines, finding_line, goals_met, seen

# films never go in git; the demo folder is the closeout's; the PR body links the record and is not in it
NOT_IN_RECORD = re.compile(r"(^|/)demo/|\.(webm|mp4)$|^pr-body\.md$")


def branch_for(run_id):
    return f"evidence/{run_id}"


def url(repo, commit, run_id, rel):
    """The address of one file of the record at its exact revision."""
    return f"https://github.com/{repo}/blob/{commit}/evidence/{run_id}/{rel}"


# ── what the record says ───────────────────────────────────────────────


def retired_tests(wt, base_sha, note):
    """The test files the diff deleted, each with the replacement the builder's note names
    (`tests` row `replaces` with `failed_before`) or none. Measured from the diff, never from
    the note alone: a note that forgets a retired test is how 26 went unproven."""
    rc, o, _ = gitops.git(wt, "diff", "--name-only", "--diff-filter=D", f"{base_sha}..HEAD")
    gone = [p.strip() for p in (o or "").splitlines() if p.strip() and is_test_path(p.strip())] if rc == 0 else []
    named = {str(t.get("replaces")): t for t in (note or {}).get("tests") or [] if t.get("replaces")}
    rows = []
    for path in gone:
        t = named.get(path)
        proven = bool(t and str(t.get("failed_before") or "").strip())
        rows.append({"path": path, "replacement": (t or {}).get("path"), "failed_before": (t or {}).get("failed_before"),
                     "proven": proven})
    return rows


def _identity(r):
    i = r.get("identity") or {}
    return ", ".join(f"{k} {str(i.get(k))[:9]}" for k in policy.IDENTITY_KEYS if i.get(k)) or "no identity"


def markdown(contract, tip, run_id, gates, review, receipts, note, finish, preexisting=None, answers=None,
             codex_fact=None, goals=None, slices=None, extra=None, pictures_rel=None, retired=None,
             before=None, evidence=None, harness=None, allowance=None, stops=None):
    """record.md, the run's one record: what is the owner's to rule, every item, each check,
    every cause, the pictures by relative path, the retired tests and the builder's decisions.
    Nothing here is bounded; the PR is."""
    note = note or {}
    receipts = receipts or []
    items = list(contract.get("items") or []) + list(goals or [])
    plain = lambda i: _plain(i, items)
    ordered = sorted(receipts, key=lambda r: (0 if r.get("state") in pictures.RED else 1, str(r.get("item"))))
    rec = [f"# Record of run {run_id}", "",
           f"Finish state: {finish.get('state')}; record {finish.get('record')}; run {finish.get('run')}; "
           f"merge {finish.get('merge')}" + (f" ({'; '.join(finish.get('why') or [])})" if finish.get("why") else ""),
           f"Code tip {tip[:9]}; harness {harness or 'unknown'}.", ""]
    rule = [f"- {x}" for x in firstscreen.yours_to_rule(stops)]
    rec += ["## Yours to rule", ""] + (rule or ["- none"]) + [""]
    decided = firstscreen.decided_for_you(note, answers)
    if decided:
        rec += ["## Decided for you (an answer overrules)", ""] + [f"- {x}" for x in decided] + [""]
    rec += ["## Every item", ""]
    if goals:
        met, total, grey = goals_met(goals, receipts)
        rec += [f"GOALS MET {met}/{total}" + (f" ({grey} not testable here)" if grey else ""), ""]
    rec += ["| item | verdict | what was seen | measured under |", "| --- | --- | --- | --- |"]
    rec += [f"| {plain(r.get('item'))} | {r.get('state')} | {seen(r, 150)} | {_identity(r)} |" for r in ordered]
    rec.append("")
    rec += ["## Items by id", ""]
    for r in ordered:
        st = r.get("state", "") + (("/" + str(r["substate"])) if r.get("substate") else "")
        rec.append(f"- {r.get('item')} {st}: {(r.get('observed') or '')[:300]}")
    rec.append("")
    if slices:
        rec += ["## Slices", "", "| issue | verdict | tip | WORKS | BROKEN | UNMEASURED |", "| --- | --- | --- | --- | --- | --- |"]
        for s in slices:
            rc = s.get("receipts") or {}
            rec.append(f"| #{s.get('issue')} | {s.get('verdict')} | {(s.get('tip') or '')[:9]} | {rc.get('WORKS', 0)} | {rc.get('BROKEN', 0)} | {rc.get('UNMEASURED', 0)} |")
        rec.append("")
    if contract.get("success"):
        rec += ["## Success (read after the merge)", ""] + [f"- {x}" for x in success.lines(contract["success"])] + [""]
    rec += pictures.markdown(pictures_rel, items, receipts, plain)
    nums = [f"- {plain(r.get('item'))}: {seen(r, 160)}" for r in receipts
            if (next((x for x in items if x.get("item") == r.get("item")), None) or {}).get("kind") == "measure"]
    if nums:
        rec += ["## Numbers", ""] + nums + [""]
    if evidence:
        videos = _all_videos(evidence, receipts)
        if videos:
            rec += ["## Films (sent by the relay; not in git)", ""] + [f"- {plain(i)}: {Path(v).name}" for i, v in videos] + [""]
    rec += _answers_lines(answers)
    befores = [(it.get("item"), it.get("before")) for it in note.get("items") or [] if it.get("before")]
    if befores:
        rec += ["## What was wrong", ""] + [f"- {i}: {b}" for i, b in befores] + [""]
    left = []
    for f in (review or {}).get("findings") or []:
        if f.get("status") == "open":
            left.append(f"- review {finding_line(f)}")
    for it in note.get("items") or []:
        if it.get("status") in ("left-out", "blocked"):
            left.append(f"- {it.get('item')} {it.get('status')}: {(it.get('because') or '')[:240]}")
    if left:
        rec += ["## Left open", ""] + left + [""]
    parity = [g for g in gates or [] if g.get("ci_parity")]
    if parity:
        rec += ["## CI parity", ""]
        for g in parity:
            rec.append(f"- {g.get('name')}: " + (f"UNMEASURED ({g['unmeasured']})" if g.get("true_exit") is None
                                                 else f"TRUE_EXIT={g.get('true_exit')}"))
        rec.append("")
    rec += ["## Gates", ""]
    for g in gates or []:
        if g.get("ci_parity"):
            continue
        rec.append(f"- {g.get('name')}: TRUE_EXIT={g.get('true_exit')}" + (f" (UNMEASURED: {g['unmeasured']})" if g.get("unmeasured") else "")
                   + (f" ({g['fallback']})" if g.get("fallback") else ""))
    for p in preexisting or []:
        rec.append(f"- pre-existing red, not this change's: {p.get('gate')}: {str(p.get('evidence') or p.get('why') or '')[:200]}")
    rec.append(f"- second reader: {codex_fact or 'none: not run'}")
    rec.append("")
    rec += firstscreen.audit_lines((allowance or {}).get("audit"), (allowance or {}).get("minutes"))
    rec += ["## Retired tests", ""]
    for t in retired or []:
        rec.append(f"- `{t['path']}`: " + (f"replaced by `{t['replacement']}`, shown failing before: {t['failed_before']}" if t["proven"]
                                           else "NO REPLACEMENT shown failing on the same defect"))
    rec += (["- none"] if not retired else []) + [""]
    decisions = note.get("decisions") or []
    if decisions:
        rec += ["## Decisions (the builder's note)", ""] + [f"- {d.get('text')}" + (f" ({d['ruling']})" if d.get("ruling") else "") for d in decisions] + [""]
    tests = note.get("tests") or []
    if tests:
        rec += ["## Tests", ""] + [f"- `{t.get('path')}`: {t.get('pins')}" for t in tests] + [""]
    for title, rows in (extra or []):
        rec += [f"## {title}", ""] + [f"- {x}" for x in rows] + [""]
    skipped = adapter.not_offered_lines()
    if skipped:
        rec += ["## Not offered by the venue", ""] + [f"- {x}" for x in skipped] + [""]
    return "\n".join(rec)


def _plain(item, items):
    it = next((x for x in items or [] if x.get("item") == item), None)
    if not it:
        return item
    then = re.sub(r"\s+", " ", it.get("then") or "").strip()
    return (then if len(then) <= 90 else then[:90].rsplit(" ", 1)[0] + "…") or item


def _all_videos(evidence, receipts):
    """Every film the run holds: the latest receipt's per item first, then any film an
    earlier drive left under drives/, one path per item."""
    done, out = set(), []
    for r in receipts or []:
        if r.get("video") and r.get("item") not in done:
            done.add(r["item"]); out.append((r["item"], r["video"]))
    drives = sorted((Path(evidence) / "drives").glob("verify-*"),
                    key=lambda p: -int(p.name.split("-")[-1]) if p.name.split("-")[-1].isdigit() else 0)
    for d in drives:
        for f in sorted((d / "video").glob("*.webm")) if (d / "video").is_dir() else []:
            if f.stem not in done:
                done.add(f.stem); out.append((f.stem, str(f)))
    return out


# ── the files, the commit ───────────────────────────────────────────────


def files_of(j, evidence, pics):
    """{relative name in the record: local path}: every file under evidence but the films,
    the demo and the PR body; a carried picture from a slice's folder under its own name;
    each seat's report."""
    evidence = Path(evidence)
    out = {}
    for p in sorted(evidence.rglob("*")):
        if p.is_file():
            rel = p.relative_to(evidence).as_posix()
            if not NOT_IN_RECORD.search(rel):
                out[rel] = p
    for rel, path in pictures.names(pics or {}).items():
        out.setdefault(rel, Path(path))
    for rep in sorted((j.dir / "seats").glob("*/report.json")):
        out[f"seats/{rep.parent.name}/report.json"] = rep
    return out


def references(run_dir):
    """The paths under the run folder the record lists (`record.json`), as it names them."""
    try:
        files = json.loads(report_text(Path(run_dir) / "evidence" / "record.json")).get("files") or []
    except (OSError, ValueError):
        return set()
    return {f if f.startswith("seats/") else f"evidence/{f}" for f in files} | {"evidence/record.json"}


def _tree(wt, run_id, blobs):
    """A tree holding `evidence/<run_id>/<rel>` for each (rel, blob), through a throwaway index."""
    fd, index = tempfile.mkstemp(prefix="hb-record-")
    os.close(fd); os.unlink(index)
    env = {**os.environ, "GIT_INDEX_FILE": index}
    try:
        entries = "".join(f"100644 {blob}\t{'evidence'}/{run_id}/{rel}\0" for rel, blob in blobs)
        rc, _, e = run(["git", "-C", str(wt), "update-index", "-z", "--add", "--index-info"], timeout=120, input=entries, env=env)
        if rc != 0:
            return None, f"update-index: {e[-200:]}"
        rc, tree, e = run(["git", "-C", str(wt), "write-tree"], timeout=60, env=env)
        return (tree.strip(), None) if rc == 0 else (None, f"write-tree: {e[-200:]}")
    finally:
        try:
            os.unlink(index)
        except OSError:
            pass


def _parent(wt, branch):
    """The branch's tip as origin holds it, else as this clone holds it, else none."""
    if gitops.git(wt, "fetch", "-q", "origin", f"refs/heads/{branch}", timeout=300)[0] == 0:
        return gitops.git(wt, "rev-parse", "FETCH_HEAD")[1].strip() or None
    rc, o, _ = gitops.git(wt, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return o.strip() if rc == 0 and o.strip() else None


def publish(j, wt, c, evidence, pics, shadow):
    """One commit on `evidence/<run_id>` holding the record's files, parented on the previous
    publish, pushed without force; in shadow the commit is made and not pushed. Journaled as
    the `record-published` finish state. Returns {commit, url, pushed, files} or {error}."""
    evidence = Path(evidence)
    files = files_of(j, evidence, pics)
    (evidence / "record.json").write_text(json.dumps({"run_id": j.run_id, "files": sorted(files)}, indent=2), encoding="utf-8")
    files["record.json"] = evidence / "record.json"
    blobs = []
    for rel, path in sorted(files.items()):
        rc, blob, e = gitops.git(wt, "hash-object", "-w", str(path))
        if rc != 0:
            j.append("finish", state="record-published", record="unpublished", error=f"hash-object {rel}: {e[-200:]}")
            return {"error": f"hash-object {rel}: {e[-200:]}"}
        blobs.append((rel, blob.strip()))
    branch = branch_for(j.run_id)

    def do():
        tree, err = _tree(wt, j.run_id, blobs)
        if err:
            return {"error": err}
        parent = _parent(wt, branch)
        rc, sha, e = run(["git", "-C", str(wt), "-c", "user.name=hb", "-c", "user.email=hb@local", "commit-tree", tree,
                          *(["-p", parent] if parent else []), "-m", f"evidence: the record of run {j.run_id}"], timeout=60)
        if rc != 0:
            return {"error": f"commit-tree: {e[-200:]}"}
        sha = sha.strip()
        gitops.git(wt, "update-ref", f"refs/heads/{branch}", sha)
        if shadow:
            return {"commit": sha, "pushed": False, "files": len(blobs)}
        rc, o, e = gitops.git(wt, "push", "-q", "origin", f"{sha}:refs/heads/{branch}", timeout=300)
        return {"commit": sha, "pushed": True, "files": len(blobs)} if rc == 0 else {"error": (o + e)[-300:]}
    key = "record@" + hashlib.sha1("\n".join(f"{r}:{b}" for r, b in blobs).encode("utf-8")).hexdigest()[:12]
    res = j.op(key, do)
    if not res.get("commit"):
        j.append("finish", state="record-published", record="unpublished", error=res.get("error"))
        return res
    link = url(c["repo"], res["commit"], j.run_id, "record.md") if c.get("repo") else None
    j.append("finish", state="record-published", record="published", commit=res["commit"], pushed=res.get("pushed"), url=link)
    j.record(evidence_branch=branch, evidence_commit=res["commit"], record_url=link)
    return dict(res, url=link, branch=branch)


# ── the landing, the same for every beat ───────────────────────────────


def write_local(j, wt, contract, evidence, base_sha, note, stops, gates=(), review=None, receipts=(), **md):
    """record.md for a run that ends before it lands (stopped for the owner, or a slice
    inside a wave ending `built`): the same one record, written in place, never published.
    Returns its path."""
    finish = firstscreen.finish_state(stops, list(receipts or []), contract.get("items") or [], "unpublished",
                                 gates=gates, review=review, harness=j.fact("harness"))
    path = Path(evidence) / "record.md"
    path.write_text(markdown(contract, gitops.head_of(wt) or "", j.run_id, list(gates or []), review, list(receipts or []),
                             note, finish, harness=j.fact("harness"), stops=stops, **md), encoding="utf-8")
    return str(path)


def land(j, wt, c, contract, evidence, base_sha, branch, receipts, gates, preexisting, review, note, shadow, stops,
         goals=(), extra_why=(), **md):
    """Write record.md, publish the record, decide the three finish states and journal them,
    all before any PR is written. `md` are markdown's extra facts (slices, extra, answers,
    codex_fact, before, allowance). Returns (finish, published, retired)."""
    items = list(contract.get("items") or []) + list(goals or [])
    evs = j.events()
    harness = j.fact("harness")
    how = next((e.get("result") for e in reversed(evs) if e.get("kind") == "loop" and e.get("result")), None)
    retired = retired_tests(wt, base_sha, note)
    pics = pictures.collect(evidence, receipts, items)
    # the scores ride the record, where the next run reads the last one; the success block
    # rides the phase view, where the closeout reads it after the merge
    battery.write_scores(evidence, receipts, gitops.head_of(wt), j.run_id, base_sha)
    md = dict(md, extra=list(md.get("extra") or []) + battery.report_lines(receipts) or None)
    if contract.get("success"):
        j.record(success=contract["success"])
    facts = dict(gates=gates, preexisting=preexisting, review=review, goals=goals, extra_why=extra_why, loop_end=how,
                 changed=str(gitops.head_of(wt) or "") != str(base_sha or ""),
                 harness=harness)
    draft = firstscreen.finish_state(stops, receipts, items, "unpublished", **facts)
    (evidence / "record.md").write_text(
        markdown(contract, gitops.head_of(wt), j.run_id, gates, review, receipts, note, draft, preexisting=preexisting,
                 goals=goals, pictures_rel=pictures.relative(pics), retired=retired, evidence=evidence,
                 harness=harness, stops=stops, **md), encoding="utf-8")
    published = publish(j, wt, c, evidence, pics, shadow) if c.get("repo") else {"error": "no repo"}
    fs = firstscreen.finish_state(stops, receipts, items, "published" if published.get("commit") else "unpublished", **facts)
    j.append("finish", state=RUN_COMPLETE if fs["run"] == "complete" else "run-stopped", run=fs["run"], how=how)
    j.append("finish", state=fs["merge_state"], merge=fs["merge"], why=fs["why"])
    return fs, published, retired
