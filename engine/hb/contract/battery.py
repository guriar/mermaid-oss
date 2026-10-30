"""hb.run.battery - the battery is the drive for a chat item (ruling 14).

Every contract item `held_by: model` is graded here and nowhere else. One that names
`battery: <case set id>` takes its number from the golden set run k times and graded by code
on the call, the card and the venue state, never from a tester's prose. The harness reads the
battery through one contract (docs/battery-contract.md): the runner command the product's
`harness-config.json` names under `battery.command`, given the case set, the venue and k,
prints one JSON line per case try and one summary line. The harness checks that output
against the contract (every case, exactly k tries each, every case the set held last time)
and grades pass^k against the item's bar (`pass_rate`, default DEFAULT_RATE).

A runner crash, a rate limit, a timeout or malformed output is an environment signature
(hb.run.verdict, `battery-runner`), never BROKEN. A model item with no `battery:`, no runner
on the product, or a case set the runner does not know reads UNMEASURED `no-battery` by name;
the tester seat runs the discovery brief for every model item, and its verdict there is
`found` (new case sentences) or nothing new, never a pass rate. The battery's row is the
item's row whatever the tester seat did: a seat that crashed or left the item out never
erases it, and a find on a WORKS row turns it BROKEN naming the find, since a find is a
product red until the golden set holds it. Every run, score and find is a journal event the
reducer folds; scores live in the record (`battery-scores.json`), and the previous score is
read only from a record with the same case set, k and cases, on this branch's lineage.
"""
import json
import re
import shlex
import time
from pathlib import Path

from hb.rig import gitops
from hb.runner import hbcall

NO_BATTERY = "no battery:"
RUNNER_FAILED = "battery runner"
SCORES_FILE = "battery-scores.json"
# the runner's exit codes, as the contract names them; 2 is argparse's usage error, never a set's
EXIT_RAN, EXIT_ENVIRONMENT, EXIT_UNKNOWN_SET = 0, 3, 4
# a model item that declares no rate is held to the floor the sittings file
DEFAULT_RATE = "4/5"


def is_model(item):
    return str((item or {}).get("held_by") or "").strip() == "model"


def case_set(item):
    return str((item or {}).get("battery") or "").strip()


def rate_of(item):
    return (item or {}).get("pass_rate") or DEFAULT_RATE


def parse_rate(rate):
    """(N, M) from "N/M", or None when the text is not a rate."""
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", str(rate or ""))
    if not m:
        return None
    n, total = int(m.group(1)), int(m.group(2))
    return (n, total) if 0 < n <= total else None


def bar(item):
    """The item's bar as a fraction of cases that must pass all k tries."""
    n, m = parse_rate(rate_of(item)) or parse_rate(DEFAULT_RATE)
    return n / m


def command(c):
    """The runner command as argv, or None when the product names none."""
    cmd = str(((c or {}).get("battery") or {}).get("command") or "").strip()
    return shlex.split(cmd) if cmd else None


def try_ok(row):
    """One try passes when the call, the card and the venue state were right and the reply's
    words were not judged off. A missing key is malformed output, not a failed try."""
    for key in ("call_ok", "card_ok", "state_ok"):
        if not isinstance(row.get(key), bool):
            raise ValueError(f"case {row.get('case')} try {row.get('try')}: `{key}` is not a boolean")
    return row["call_ok"] and row["card_ok"] and row["state_ok"] and row.get("reply_judge") != "off"


def parse_output(text):
    """(case rows, summary) from the runner's stdout; a line that is not JSON is skipped,
    since a runner may print its own progress. Raises ValueError when no summary line came."""
    cases, summary = [], None
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if "case" in row:
            cases.append(row)
        elif "cases" in row and "k" in row:
            summary = row
    if summary is None:
        raise ValueError("no summary line")
    return cases, summary


def validate(cases, summary, k, known):
    """{case: [try ok, ...]} once the output holds what the contract promises: at least one
    case, as many as the summary says, exactly k tries each (`k` asked, else the summary's),
    no try the runner marked rate limited, and every case of `known` (the set's cases on its
    last record). Raises ValueError naming what is missing; a runner that drops an errored
    case would otherwise raise pass^k."""
    by_case = {}
    for row in cases:
        if row.get("rate_limited"):
            raise ValueError(f"case {row.get('case')} try {row.get('try')} was rate limited, not measured")
        by_case.setdefault(str(row.get("case")), []).append(try_ok(row))
    if not by_case:
        raise ValueError("zero case lines")
    if summary.get("cases") != len(by_case):
        raise ValueError(f"the summary says {summary.get('cases')} cases and {len(by_case)} were printed")
    want = int(k or summary.get("k") or 0)
    short = sorted(cid for cid, tries in by_case.items() if len(tries) != want)
    if want < 1 or short:
        raise ValueError(f"k is {want} and these cases ran another count of tries: {', '.join(short[:5]) or 'all'}")
    dropped = sorted(set(known or []) - set(by_case))
    if dropped:
        raise ValueError(f"cases the set held on its last record are missing: {', '.join(dropped[:5])}")
    return by_case


def grade(by_case, cases, summary, item, k):
    """{pass_k, k, cases, case_ids, failed, says} from the validated case lines: a case passes
    when every try passed; pass^k is the share of cases that did. The summary's own pass_k is
    the runner's word and is kept beside, never graded from."""
    says = {str(r.get("case")): str(r["say"]) for r in cases if r.get("say")}
    failed = sorted(cid for cid, tries in by_case.items() if not all(tries))
    total = len(by_case)
    return {"set": case_set(item), "k": int(k or summary.get("k")), "cases": total, "case_ids": sorted(by_case),
            "failed": failed, "pass_k": round((total - len(failed)) / total, 4), "bar": rate_of(item), "says": says,
            "ask_rate": summary.get("ask_rate"), "extra_calls": summary.get("extra_calls"),
            "runner_pass_k": summary.get("pass_k")}


def _row(item, state, substate, observed, venue, **extra):
    return {"item": item["item"], "state": state, "substate": substate, "observed": observed, "seen": observed,
            "situation": f"the golden case set {case_set(item) or '(none named)'} on the venue",
            "paths_tried": ["battery"], "scenario": None, "capture": None, "video": None, "plan": None,
            "tenant_id": (venue or {}).get("tenant_id"), "no_venue": None if (venue or {}).get("tenant_id") else "the battery runner signed in itself",
            "evidence": [], **extra}


def _no_battery(j, item, venue, code, why, evidence=None, exit_code=None):
    """The UNMEASURED no-battery row; `code` is the journal's short reason, `why` the row's sentence."""
    j.append("battery-run", item=item["item"], set=case_set(item) or None, state="no-battery", why=code, exit=exit_code)
    return _row(item, "UNMEASURED", "no-battery", f"{NO_BATTERY} {why}; the tester seat drove discovery only", venue,
                evidence=evidence or []), {}


def new_only(c):
    """True when the battery runs only the golden cases a change adds, once at the audit
    (`battery.scope: new`): the owner's bound on model spend (Q4)."""
    return str(((c or {}).get("battery") or {}).get("scope") or "") == "new"


def new_case_ids(wt, c, base_sha, set_id):
    """The case ids of `set_id` the golden file holds at the tip and not at `base_sha`, sorted;
    None when there is no golden file to read."""
    path = golden_path(wt, c)
    if not path or not path.is_file() or not base_sha:
        return None
    tip, _, _ = _read_golden(path)
    rc, text, _ = gitops.git(wt, "show", f"{base_sha}:{path.relative_to(Path(wt)).as_posix()}")
    try:
        base = json.loads(text) if rc == 0 and text.strip() else {}
    except ValueError:
        base = {}
    had = {case.get("id") for case in base.get("cases") or []}
    return sorted(case["id"] for case in tip.get("cases") or []
                  if case.get("family") == set_id and case.get("id") not in had)


def _argv(cmd, item, venue, k, out_dir):
    argv = list(cmd) + ["--set", case_set(item), "--out", str(out_dir)]
    if item.get("ids"):
        argv += ["--ids", ",".join(item["ids"])]
    for flag, key in (("--origin", "origin"), ("--env-file", "env_file"), ("--tenant-id", "tenant_id")):
        if (venue or {}).get(key):
            argv += [flag, str(venue[key])]
    if k:
        argv += ["--k", str(k)]
    return argv


def _names_unknown_set(text, set_id):
    """True when the runner's last JSON line names the set it does not know."""
    return str(hbcall.last_json(text).get("unknown_set") or "") == set_id


def run_one(j, c, item, venue, wt, evidence, timeout_min, base_sha):
    """(receipt row, {case: say}) for one model item: the runner run once under admission,
    its output kept under the evidence folder, checked against the contract, graded, and the
    `battery-run` event written."""
    if not case_set(item):
        return _no_battery(j, item, venue, "no case set", "the item names no golden case set (`battery:`)")
    cmd = command(c)
    if not cmd:
        return _no_battery(j, item, venue, "no runner", f"the product names no battery runner (harness-config `battery.command`); "
                                           f"case set {case_set(item)} was not run")
    if item.get("ids") is not None and not item["ids"]:
        return _no_battery(j, item, venue, "no new cases", f"the change adds no golden case to set {case_set(item)}; "
                           "the battery runs only the cases a change adds")
    out_dir = Path(evidence or j.dir / "evidence") / "battery" / item["item"]
    out_dir.mkdir(parents=True, exist_ok=True)
    k = item.get("k") or ((c or {}).get("battery") or {}).get("k")
    t0 = time.time()
    rc, o, e = hbcall.call(_argv(cmd, item, venue, k, out_dir), cwd=wt, timeout=timeout_min * 60,
                           kind="battery", holder=f"{j.run_id} battery {item['item']}", c=c, note=j.append)
    (out_dir / "runner.out").write_text(o or "", encoding="utf-8")
    (out_dir / "runner.err").write_text(e or "", encoding="utf-8")
    took = round((time.time() - t0) / 60, 2)
    evidence_paths = [str(out_dir / "runner.out")]
    if rc == EXIT_UNKNOWN_SET and _names_unknown_set(o, case_set(item)):
        return _no_battery(j, item, venue, "unknown case set", f"the runner does not know case set {case_set(item)}",
                           evidence=evidence_paths, exit_code=rc)

    def failed(why, **facts):
        j.append("battery-run", item=item["item"], set=case_set(item), state="runner-failed", why=why, minutes=took, **facts)
        return _row(item, "UNMEASURED", "battery-runner", f"{RUNNER_FAILED} {why}", venue, evidence=evidence_paths), {}
    if rc != EXIT_RAN:
        kind = {EXIT_ENVIRONMENT: "rate limited or the venue unreachable", 124: "timed out", 2: "refused its arguments"}.get(rc, "crashed")
        return failed(f"{kind} (exit {rc}): {(e or o)[-200:]}", exit=rc)
    # a run of the added cases alone is not the whole set, so no earlier record's cases are owed
    known = None if item.get("ids") else (last_record(wt, case_set(item), base_sha) or {}).get("case_ids")
    try:
        cases, summary = parse_output(o)
        score = grade(validate(cases, summary, k, known), cases, summary, item, k)
    except ValueError as ex:
        return failed(f"output malformed: {ex}")
    score["previous"] = previous_score(wt, score, base_sha)
    score["minutes"] = took
    passed = score["pass_k"] >= bar(item)
    observed = (f"battery {score['set']}: pass^k {score['pass_k']} over {score['cases']} cases, k {score['k']}, "
                f"bar {score['bar']}" + (f"; failed: {', '.join(score['failed'])}" if score["failed"] else ""))
    row = _row(item, "WORKS" if passed else "BROKEN", None if passed else "failed", observed, venue,
               evidence=evidence_paths, battery={k_: v for k_, v in score.items() if k_ != "says"})
    j.append("battery-run", item=item["item"], set=score["set"], state=row["state"], pass_k=score["pass_k"], k=score["k"],
             cases=score["cases"], failed=score["failed"], bar=score["bar"], previous=score["previous"], minutes=took)
    return row, score["says"]


def run_all(j, c, items, venue, wt, evidence, timeout_min, base_sha):
    """{item id: (row, says)} for every model item, each run once before any tester sits, so
    the discovery brief can list the golden cases the battery holds."""
    return {it["item"]: run_one(j, c, it, venue, wt, evidence, timeout_min, base_sha)
            for it in items or [] if is_model(it)}


def golden_rows(items, scores):
    """Each model item with the golden cases the battery holds for it, for the discovery seat."""
    rows = []
    for it in items or []:
        if not is_model(it):
            continue
        row, says = scores.get(it["item"], (None, {}))
        rows.append({"item": it["item"], "case_set": case_set(it) or None, "act": it.get("then"),
                     "battery": (row or {}).get("observed") or "not run",
                     "golden_cases": [f"{cid}: {s}" for cid, s in sorted(says.items())] or "none listed"})
    return rows


def fold(rows, items, scores, j, finds=None):
    """The rows with the battery's row standing for every model item of `items`, whatever the
    tester seat wrote, crashed on or left out. The tester's `found` rides the row and is
    journaled as `tester-find`; a find on a WORKS row makes it BROKEN naming the find.
    `finds` are the discovery seat's; each model item's row carries how many it proposed."""
    model = [it for it in items or [] if is_model(it)]
    tester = {str(r.get("item")): r for r in rows or []}
    ours = {it["item"]: dict(_measured(it, tester.get(it["item"]), scores, j),
                             proposed=sum(1 for f in finds or [] if f.get("item") == it["item"])) for it in model}
    out = [ours.get(str(r.get("item")), r) for r in rows or []]
    return out + [ours[it["item"]] for it in model if it["item"] not in tester]


def _measured(item, tester_row, scores, j):
    found = [str(f) for f in ((tester_row or {}).get("found") or []) if str(f).strip()]
    row, _ = scores.get(item["item"]) or (None, {})
    if row is None:
        row = _row(item, "UNMEASURED", "no-battery", f"{NO_BATTERY} the battery did not run for this item", None)
    if tester_row is not None:
        j.append("tester-find", item=item["item"], set=case_set(item) or None, found=found)
    row = dict(row, found=found)
    if found and row["state"] == "WORKS":
        row.update(state="BROKEN", substate="failed",
                   observed=f"{row['observed']}; the tester broke the act in a case the golden set does not hold: {found[0]}")
    return row


# ── the golden file: frozen cases and proposed ones (Q4) ─────────────────────


def golden_path(wt, c):
    """The product's golden file, named under `battery.golden`, or None."""
    rel = str(((c or {}).get("battery") or {}).get("golden") or "").strip()
    return Path(wt) / rel if rel else None


def _read_golden(path):
    """(parsed, text with LF newlines, the newline the file uses)."""
    raw = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n")
    return json.loads(text), text, newline


def golden_says(wt, c, set_id):
    """{case id: the user's words} for every frozen case of a set, read from the golden file."""
    path = golden_path(wt, c)
    if not set_id or not path or not path.exists():
        return {}
    golden, _, _ = _read_golden(path)
    return {case["id"]: " / ".join(s["say"] for s in case.get("steps") or [] if s.get("say"))
            for case in golden.get("cases") or [] if case.get("family") == set_id}


def _proposed_case(find):
    words = re.sub(r"[^a-z0-9]+", "-", find["said"].lower()).strip("-")[:60]
    return {"id": f"proposed-{words}", "family": find["set"], "shape": "one-act", "shop_state": "shop-as-born",
            "steps": [{"say": find["said"]}], "expect": {}, "origin": "discovery find", "note": find["wrong"],
            "status": "proposed"}


def propose_golden(finds, wt, c):
    """(written count, why) after appending each find that names a case set to the golden file's
    `proposed` list under that set, never to the frozen `cases`; the reviewer accepts them. A file
    the harness cannot rewrite byte for byte is left alone, so a frozen case never moves."""
    path = golden_path(wt, c)
    if not path or not path.exists():
        return 0, "the product names no golden file (harness-config `battery.golden`)"
    golden, text, newline = _read_golden(path)
    second_line = (text.split("\n") + [""])[1]
    indent = len(second_line) - len(second_line.lstrip(" ")) or 1
    if json.dumps(golden, indent=indent, ensure_ascii=False) + "\n" != text:
        return 0, f"{path.name} is not in the shape json.dumps writes, so rewriting it would move frozen cases"
    proposed = golden.setdefault("proposed", {})
    # the product may write `proposed` as one flat list; the engine reads either shape and
    # never crashes a run on a bookkeeping step
    if isinstance(proposed, list):
        held = {case["id"] for case in golden.get("cases") or []} | {case.get("id") for case in proposed}
        add = lambda _set, case: proposed.append(case)
    else:
        held = {case["id"] for case in golden.get("cases") or []} | {case["id"] for cases in proposed.values() for case in cases}
        add = lambda set_id, case: proposed.setdefault(set_id, []).append(case)
    written = 0
    for find in finds or []:
        if not find.get("set"):
            continue
        case = _proposed_case(find)
        if case["id"] in held:
            continue
        add(find["set"], case)
        held.add(case["id"])
        written += 1
    if written:
        out = json.dumps(golden, indent=indent, ensure_ascii=False) + "\n"
        path.write_bytes(out.replace("\n", newline).encode("utf-8"))
    return written, None


# ── scores in the record ─────────────────────────────────────────────────────


def write_scores(evidence, receipts, tip, run_id, base_sha=None):
    """battery-scores.json beside the record: one entry per case set measured on this run,
    with what a later comparison must agree on (k, the case ids) and where it stood."""
    scores = {}
    for r in receipts or []:
        b = r.get("battery")
        if b and b.get("set"):
            scores[b["set"]] = {**{k: b.get(k) for k in ("pass_k", "k", "cases", "case_ids", "failed", "bar")},
                                "tip": tip, "base": base_sha, "run_id": run_id}
    if scores:
        (Path(evidence) / SCORES_FILE).write_text(json.dumps(scores, indent=2), encoding="utf-8")
    return scores


def _records(wt, set_id):
    """(commit, entry) for every record commit that scored `set_id`, newest first."""
    rc, o, _ = gitops.git(wt, "log", "--all", "--format=%H", "--name-only", "--", f"evidence/*/{SCORES_FILE}")
    if rc != 0 or not o.strip():
        return
    sha = None
    for line in o.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.endswith(SCORES_FILE):
            sha = line
            continue
        rc, text, _ = gitops.git(wt, "show", f"{sha}:{line}")
        try:
            hit = (json.loads(text) if rc == 0 else {}).get(set_id)
        except ValueError:
            hit = None
        if hit:
            yield sha, hit


def _on_lineage(wt, tip, base_sha):
    """True when the record's tip is in this branch's history at or before the PR's base."""
    if not tip or not base_sha:
        return False
    rc, _, _ = gitops.git(wt, "merge-base", "--is-ancestor", str(tip), str(base_sha))
    return rc == 0


def last_record(wt, set_id, base_sha):
    """The newest record of `set_id` on this branch's lineage, whatever its k: the cases the set
    held last time, which a run must not drop."""
    for sha, hit in _records(wt, set_id):
        if _on_lineage(wt, hit.get("tip"), base_sha):
            return dict(hit, commit=sha)
    return None


def previous_score(wt, score, base_sha):
    """The score this one is compared with: the newest record of the same set, k and cases
    whose tip is on this branch's lineage (an ancestor of the PR's base), never a sibling
    branch's, another k's or a grown set's."""
    for sha, hit in _records(wt, score["set"]):
        if (hit.get("k") == score["k"] and sorted(hit.get("case_ids") or []) == score["case_ids"]
                and _on_lineage(wt, hit.get("tip"), base_sha)):
            return dict(hit, commit=sha)
    return None


def report_lines(receipts):
    """The record's battery section: pass^k per item against the last comparable score, the
    model items with no battery by name, and every tester find as a proposed golden case."""
    scores, finds = [], []
    for r in receipts or []:
        b = r.get("battery")
        if b:
            prev = b.get("previous") or {}
            last = (f"last {prev.get('pass_k')} at {str(prev.get('tip') or '')[:9]}" if prev else "no comparable earlier score")
            scores.append(f"{r.get('item')} {b.get('set')}: pass^k {b.get('pass_k')} (k {b.get('k')}, {b.get('cases')} cases, "
                          f"bar {b.get('bar')}), {last}" + (f"; failed {', '.join(b.get('failed') or [])}" if b.get("failed") else ""))
        elif str(r.get("observed") or "").startswith(NO_BATTERY):
            scores.append(f"{r.get('item')}: {r.get('observed')}")
        for f in r.get("found") or []:
            finds.append(f"{r.get('item')}: {f}")
    sections = []
    if scores:
        sections.append(("Battery, pass^k against the last comparable score", scores))
    if finds:
        sections.append(("Proposed golden cases (tester finds, for the golden file)", finds))
    return sections
