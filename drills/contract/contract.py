#!/usr/bin/env python
"""Drills for hb.run.contract, the resolver behind an issue number.

Offline: gh is the drill stub, the manifest and the mock are written into a temp tree.
Run: python engine/drill_contract.py
"""
import hashlib
import os
import sys
import tempfile
from pathlib import Path
os.environ.setdefault("HB_MEMORY_FREE_GB", "64")  # the drills measure the engine, never this host's memory
os.environ.setdefault("HB_MEMORY_TOTAL_GB", "32")
os.environ.setdefault("HB_SETTLE_S", "0.2")
os.environ.setdefault("HB_SETTLE_MAX_S", "1")
os.environ.setdefault("HB_FORCE_AFTER_S", "1")

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
FIXTURE = ROOT / "drills" / "fixture"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))
sys.path.insert(0, str(ENGINE))

PASS, FAIL = [], []


# the interpreter the drill runs under, forward slashes as a command string is written
PY = sys.executable.replace(chr(92), '/')

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(f"{name}{(': ' + detail) if detail and not cond else ''}")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def write(p, text):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def stub_gh(tmp, body, title="a slice"):
    """A gh stand-in that answers `issue view` with this body."""
    import json
    script = tmp / "gh_stub.py"
    payload = json.dumps({"title": title, "body": body, "labels": [{"name": "slice"}]})
    write(script, "import sys, json\n"
                  "argv = sys.argv[1:]\n"
                  "if argv[:2] == ['issue', 'view']:\n"
                  f"    print({payload!r}); sys.exit(0)\n"
                  "print(json.dumps({'stub': True})); sys.exit(0)\n")
    os.environ["HB_GH_STUB"] = str(script)


MANIFEST = """schema: "manifest/v1"
wave: "t"
id_prefix: ""
precedence:
  default: "artifact-first"
artifacts:
  - path: "design/t/mock.html"
    sha256: "{mock_sha}"
fixture_recipes:
  - id: "r1"
    steps:
      - "open the venue"
items:
  - id: "T-01"
    rev: 1
    kind: "api"
    slice: "t"
    fixture_recipe_id: "r1"
    when: "a caller posts the delete"
    then: "the row is hidden"
    files: ["src/x.py"]
  - id: "T-02"
    rev: 1
    kind: "api"
    slice: "t"
    fixture_recipe_id: "r1"
    when: "a caller lists people"
    then: "the hidden row is absent"
    files: ["src/y.py"]
  - id: "T-03"
    rev: 1
    kind: "screen"
    slice: "t"
    fixture_recipe_id: "r1"
    when: "a user opens the drawer"
    then: "a Delete verb sits under Bring back"
    files: ["web/z.tsx"]
  - id: "T-04"
    rev: 1
    kind: "screen"
    slice: "t"
    fixture_recipe_id: "r1"
    when: "a user presses Delete"
    then: "the person leaves the Removed list"
    files: ["web/z.tsx"]
    checkpoint: true
  - id: "U-01"
    rev: 1
    kind: "api"
    slice: "other"
    fixture_recipe_id: "r1"
    when: "another slice's item"
    then: "is not ours"
"""


def make_tree(tmp):
    root = tmp / "repo"
    mock = write(root / "design" / "t" / "mock.html", "<html>mock</html>\n")
    write(root / "docs" / "manifests" / "t.yaml", MANIFEST.replace("{mock_sha}", sha(mock)))
    for f in ("src/x.py", "src/y.py", "web/z.tsx"):
        write(root / f, "\n")
    # the engine reads every path from the product's config, so the tree is a repo that carries one
    import subprocess
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    write_config(root)
    os.chdir(root)
    return root


PATHS = {"manifest_dir": "docs/manifests", "ledger_file": "docs/ledger/delivery.jsonl",
         "hotfix_dir": ".context/hotfix", "worktrees_dir": ".claude/worktrees", "changelog_file": "CHANGELOG.md"}


def write_config(root, venue=None):
    """The harness config the drill tree carries; `venue` is its venue block, none by default."""
    import json
    cfg = {"repo": "drill/drill", "state_dir": ".context/harness", "paths": PATHS}
    if venue:
        cfg["venue"] = venue
    write(root / ".claude" / "harness-config.json", json.dumps(cfg))


def venue_block(states_path):
    """A venue block whose commands are python one-liners: it declares `states` and names the catalog."""
    say = lambda text: f'{PY} -c "print(\'{text}\')"'
    return {"capabilities": say('["states"]'), "up": say("ok"), "down": say("ok"), "list": say("[]"),
            "state_build": say("ok"), "state_check": say("ok"), "state_undo": say("ok"), "states": states_path}


def main():
    from hb.contract import contract as C
    tmp = Path(tempfile.mkdtemp(prefix="hb-contract-drill-"))
    root = make_tree(tmp)
    body = ("## Intent\nwhy\n\n## Rulings\n> R1: the owner's words\n\n"
            "## Items\nT-01..T-04 in `docs/manifests/t.yaml` (authoritative), slice `t`.\n")
    # the filing shape: an id list under Items, the manifest and slice key in the trailer
    stub_gh(tmp, "## Rulings\nR9\n\n## Items\nT-02, T-03 (when/then in the manifest, authoritative).\n\n"
                 "## Tier\ndev\n\n**MANIFEST:** docs/manifests/t.yaml \u2014 authoritative; slice t.\n**ANCHOR:** #1\n")
    c = C.resolve("#7", root, require_issue=True)
    check("filing shape: the trailer names the manifest", c.get("source") == "manifest", str(c.get("problems"))[:200])
    check("filing shape: the id list under Items selects the items", [i["item"] for i in c["items"]] == ["T-02", "T-03"],
          str([i["item"] for i in c["items"]]))
    # the carve rule is the rules file's alone: an items manifest with no rules file reads as before
    check("items manifest: a list shorter than the slice resolves with no carve problem",
          not c.get("problems") and c.get("manifest", "").endswith("t.yaml"), str(c.get("problems"))[:200])
    stub_gh(tmp, "## Items\nT-02 in docs/manifests/t.yaml.\n")
    c = C.resolve("#7", root, require_issue=True)
    check("items manifest: an issue naming no slice still resolves its listed items, with no new problem",
          [i["item"] for i in c["items"]] == ["T-02"] and not c.get("problems"), str(c)[:300])
    stub_gh(tmp, "## Items\nT-02, T-99\n\n**MANIFEST:** docs/manifests/t.yaml \u2014 authoritative; slice t.\n")
    c = C.resolve("#7", root, require_issue=True)
    check("filing shape: an id the manifest lacks is a problem", any("T-99" in p for p in c.get("problems") or []),
          str(c.get("problems"))[:200])

    # 1. a manifest behind the issue
    stub_gh(tmp, body)
    c = C.resolve("#12", root)
    check("manifest: source", c["source"] == "manifest", c["source"])
    check("manifest: the slice's four items, not the fifth", [i["item"] for i in c["items"]] == ["T-01", "T-02", "T-03", "T-04"],
          str([i["item"] for i in c["items"]]))
    check("manifest: rulings verbatim", "R1: the owner's words" in c["rulings"])
    check("manifest: no problems on a fresh tree", not c["problems"], str(c["problems"])[:300])
    check("manifest: mock matched", any(ch.get("verdict") == "artifact-matched" for ch in c["checks"]), str(c["checks"]))
    text = C.render(c)
    check("render: names the manifest", "manifest:" in text)

    # 2. the mock moved under the contract
    (root / "design" / "t" / "mock.html").write_text("<html>moved</html>\n", encoding="utf-8")
    c2 = C.resolve("#12", root)
    check("stale mock: a problem", any("mock.html" in str(p) for p in c2["problems"]), str(c2["problems"])[:300])
    (root / "design" / "t" / "mock.html").write_text("<html>mock</html>\n", encoding="utf-8")

    # 3. a named file left the tree
    (root / "src" / "y.py").unlink()
    c3 = C.resolve("#12", root)
    check("missing file: a problem naming the item", any("T-02" in str(p) and "y.py" in str(p) for p in c3["problems"]),
          str(c3["problems"])[:300])
    write(root / "apps" / "backend" / "y.py", "\n")

    # 4. a hotfix that grew: the issue points at its folder
    write(root / ".context" / "hotfix" / "grown" / "contract.md",
          "# Contract: grown\n\n- HF-01 when: a person opens the page\n  then: it loads\n"
          "- HF-02 when: a person saves\n  then: it persists\n")
    stub_gh(tmp, "## Intent\ngrew\n\n## Rulings\nnone\n\n## Contract\n`.context/hotfix/grown/contract.md`\n")
    c4 = C.resolve("#13", root)
    check("hotfix folder: source", c4["source"] == "hotfix-folder", c4["source"])
    check("hotfix folder: two items", [i["item"] for i in c4["items"]] == ["HF-01", "HF-02"], str(c4["items"])[:200])

    # 5. an issue body with when/then lines and nothing else
    stub_gh(tmp, "## Rulings\nR9\n\n## Items\n- when: a user taps delete\n  then: the person is gone from the list\n")
    c5 = C.resolve("#14", root)
    check("issue body: source", c5["source"] == "issue-body", c5["source"])
    check("issue body: the pair", c5["items"][0]["then"].startswith("the person is gone"), str(c5["items"]))

    # 6. a sentence: the hotfix door takes it, the slice door refuses it
    c6 = C.resolve("On a phone the drawer scrolls", root)
    check("sentence: one item", c6["source"] == "sentence" and [i["item"] for i in c6["items"]] == ["HF-01"])
    c7 = C.resolve("On a phone the drawer scrolls", root, require_issue=True)
    check("sentence: the slice door refuses it", not c7["items"] and any("issue number" in p for p in c7["problems"]))

    # 7. an issue with nothing behind it is a problem, never a fabricated item
    stub_gh(tmp, "## Intent\njust prose that says when and then in passing\n")
    c8 = C.resolve("#15", root)
    check("empty issue: no items, one problem", not c8["items"] and len(c8["problems"]) == 1, str(c8["problems"]))

    # 8. a path outside the tree is a problem
    write(root / "docs" / "manifests" / "t.yaml",
          MANIFEST.replace("{mock_sha}", sha(root / "design" / "t" / "mock.html")).replace('files: ["src/x.py"]', 'files: ["../etc/passwd"]'))
    stub_gh(tmp, body)
    c9 = C.resolve("#12", root)
    check("outside path: a problem", any("outside the tree" in str(p) for p in c9["problems"]), str(c9["problems"])[:200])

    # 9. three proofs: each item is proven by eyes, on the model, or by the builder's test
    from hb.contract.manifest import proof_of, _manifest_schema_check
    from hb.contract import policy
    good = MANIFEST.replace("{mock_sha}", sha(root / "design" / "t" / "mock.html"))
    write(root / "docs" / "manifests" / "t.yaml", good)
    stub_gh(tmp, body)
    c10 = C.resolve("#12", root)
    proofs = {it["item"]: it["proof"] for it in c10["items"]}
    check("proofs: an api item is proven by a test, a screen by eyes",
          proofs.get("T-01") == "test" and proofs.get("T-03") == "eyes", str(proofs))
    check("proofs: a model-held item is proven on the model; a declared proof wins",
          proof_of({"kind": "chat-verb", "held_by": "model"}) == "model"
          and proof_of({"kind": "api", "proof": "eyes"}) == "eyes"
          and proof_of({"kind": "screen", "drivable": False}) == "test")
    note = {"tests": [{"path": "tests/test_hide.py", "pins": "T-01: the row is hidden"}]}
    rows = policy.proven_by_test(c10["items"], note)
    check("proofs: a test item the note pins is WORKS by test; one it does not pin is left to the drive",
          [(r["item"], r["state"], r["by"]) for r in rows] == [("T-01", "WORKS", "test")], str(rows))
    base = {"schema": "manifest/v1", "wave": "t", "id_prefix": "", "precedence": {"default": "artifact-first"},
            "fixture_recipes": [{"id": "r1", "steps": ["open the venue"]}]}
    item = {"id": "T-09", "rev": 1, "kind": "chat-verb", "slice": "t", "fixture_recipe_id": "r1",
            "when": "the user asks", "then": "the model answers", "held_by": "model", "pass_rate": "4/5"}
    said = lambda **over: " ".join(str(x) for x in _manifest_schema_check(dict(base, items=[dict(item, **over)]))[0])
    check("proofs: a model proof with no `say` is refused at filing", "needs `say`" in said(proof="model"), said(proof="model")[:200])
    check("proofs: a model proof with its words passes", "T-09" not in said(proof="model", say="who is on tomorrow?"),
          said(proof="model", say="who is on tomorrow?")[:200])
    check("proofs: an unknown proof and an undrivable eyes item are refused",
          "not one of" in said(proof="vibes") and "nobody can drive" in said(proof="eyes", held_by="server", drivable=False))

    # 10. a hotfix folder item states its own proof
    write(root / ".context" / "hotfix" / "hf" / "contract.md",
          "# hotfix\n\n## Items\n\n- HF-01 when: the verb runs\n  then: it writes one file\n  proof: test\n\n"
          "- HF-02 when: a user opens the page\n  then: the title reads Sales\n")
    hf = C._from_hotfix_folder(".context/hotfix/hf/", root)
    got = [(it["item"], it["proof"], it["then"]) for it in hf["items"]]
    check("proofs: a hotfix folder item carries its proof line, and the line leaves its then",
          got == [("HF-01", "test", "it writes one file"), ("HF-02", "eyes", "the title reads Sales")], str(got))

    # 11. a contract of rules with states: the owner signs rules, the lines are their cases
    import json as _json
    from hb.contract import rules as R
    from hb.contract.manifest import _manifest_read
    doc = {"schema": "contract/v2", "wave": "r", "rulings": ["the provider decides worked hours"],
           "states": [{"id": "sales-only", "says": "the provider for sales only", "steps": ["connect the provider with the sales scopes"]}],
           "crops": [{"id": "drawer", "path": "design/t/mock.html", "sha256": sha(root / "design" / "t" / "mock.html")}],
           "slices": [{"key": "one-answer", "landing": 1, "gets": "The user sees the same hours on every screen."}],
           "rules": [{"id": "R-CB-7", "rev": 2, "slice": "one-answer", "says": "The same week shows the same hours on every screen.",
                      "proof": "test", "states": ["all"], "crop": None,
                      "cases": [{"id": "CB-08", "when": "the week is read on Home and Schedule", "then": "the hours are the same number"}]},
                     {"id": "R-RL-1", "rev": 1, "slice": "one-answer", "says": "Role editing opens in a drawer from the right.",
                      "proof": "eyes", "states": ["sales-only"], "crop": "drawer",
                      "cases": [{"id": "RL-01", "when": "a user opens a role", "then": "its editor is in a right drawer"}]}]}
    write(root / "docs" / "manifests" / "r.json", _json.dumps(doc))
    check("rules: a well formed file is signable", R.check(doc, root) == [], str(R.check(doc, root)))
    stub_gh(tmp, "## Rulings\n\nx\n\n## Items\n\nR-CB-7, R-RL-1\n\n**MANIFEST:** docs/manifests/r.json, authoritative; slice one-answer.\n")
    c11 = C.resolve("#12", root)
    got = {it["item"]: it for it in c11["items"]}
    check("rules: each rule is one item carrying its proof, states, cases and crop",
          not c11["problems"] and got.get("R-CB-7", {}).get("proof") == "test" and got["R-CB-7"]["rev"] == 2
          and got.get("R-RL-1", {}).get("crop") == "design/t/mock.html" and got["R-RL-1"]["states"] == ["sales-only"]
          and got["R-RL-1"]["cases"][0]["id"] == "RL-01" and "the provider for sales only" in got["R-RL-1"]["when"],
          str(c11)[:500])
    check("rules: the states the rules name ride the contract for the tester", [x["id"] for x in c11.get("states") or []] == ["sales-only"],
          str(c11.get("states")))
    older, err = _manifest_read("r", root)
    check("rules: the ledger's reader sees the same rules as items", not err and [i["id"] for i in older["items"]] == ["R-CB-7", "R-RL-1"], str(err))
    # HS-03: the issue's Items line is the sitting's prose for the builder; the manifest's carve
    # alone says which rules the slice builds, so a prose range or a short list never refuses
    stub_gh(tmp, "## Rulings\n\nx\n\n## Items\n\nRules R-CB-1 to R-CB-10 in docs/manifests/r.json (slice `one-answer`).\n")
    c11b = C.resolve("#12", root)
    check("rules: a prose range in Items is never parsed; the carve is the item list",
          not c11b["problems"] and [i["item"] for i in c11b["items"]] == ["R-CB-7", "R-RL-1"], str(c11b)[:300])
    stub_gh(tmp, "## Rulings\n\nx\n\n## Items\n\nR-CB-7\n\n**MANIFEST:** docs/manifests/r.json, authoritative; slice one-answer.\n")
    c11c = C.resolve("#12", root)
    check("rules: an Items list shorter than the carve still builds the whole carve",
          not c11c["problems"] and [i["item"] for i in c11c["items"]] == ["R-CB-7", "R-RL-1"], str(c11c["problems"])[:300])
    loose = _json.loads(_json.dumps(doc))
    loose["rules"][1]["slice"] = "nobody"
    check("rules: the contract check still refuses a rule the manifest assigns to no slice",
          any("R-RL-1: slice 'nobody' is not in `slices`" in x for x in R.check(loose, root)), str(R.check(loose, root)))
    # with no carve to read, the issue's list is never the fallback
    stub_gh(tmp, "## Rulings\n\nx\n\n## Items\n\nR-CB-7\n\n**MANIFEST:** docs/manifests/r.json, authoritative; slice other-one.\n")
    c11d = C.resolve("#12", root)
    check("rules: a slice the file carves nothing for refuses, naming the key and the keys it carves",
          not c11d["items"] and any("'other-one'" in str(x) and "['one-answer']" in str(x) for x in c11d["problems"]),
          str(c11d["problems"])[:300])
    stub_gh(tmp, "## Rulings\n\nx\n\n## Items\n\nR-CB-7 in docs/manifests/r.json.\n")
    c11e = C.resolve("#12", root)
    check("rules: an issue that names no slice of a rules file refuses, saying it must name its slice",
          not c11e["items"] and any("must name its slice" in str(x) for x in c11e["problems"]), str(c11e["problems"])[:300])
    stub_gh(tmp, "## Rulings\n\nx\n\n## Items\n\nR-CB-7, R-RL-1\n\n**MANIFEST:** docs/manifests/r.json, authoritative; slice one-answer.\n")
    bad = _json.loads(_json.dumps(doc))
    bad["rules"] += [dict(bad["rules"][0], id=f"R-X-{n}", cases=[]) for n in range(10)]
    check("rules: a rule with no case is refused", any("R-X-0: no cases" in x for x in R.check(bad, root)))
    bad["rules"][1]["states"] = ["nowhere"]
    bad["rules"][0]["proof"] = "vibes"
    said = " | ".join(R.check(bad, root))
    check("rules: an unknown state and an unknown proof are each refused",
          "'nowhere' is not in `states`" in said and "'vibes'" in said, said[:400])
    moved = _json.loads(_json.dumps(doc))
    moved["crops"][0]["sha256"] = "0" * 64
    check("rules: a crop that changed since it was pinned is refused", any("changed since it was pinned" in x for x in R.check(moved, root)))
    check("rules: the filing card names what a user gets and the proof split",
          "The user sees the same hours on every screen." in R.card(doc) and "1 by eyes, 0 on the model, 1 by a test" in R.card(doc), R.card(doc)[:300])

    # 12. the studio the slice starts from, and the venue doors each eyes or model rule wears
    check("studio: a studio branch and a 7 to 40 hex commit are accepted",
          R.check(dict(doc, studio={"branch": "studio/roles", "commit": "a1b2c3d"}), root) == [])
    said = " | ".join(R.check(dict(doc, studio={"branch": "roles", "commit": "xyz"}), root))
    check("studio: a branch outside studio/ and a commit that is no sha are each refused",
          "is not studio/<slug>" in said and "7 to 40 hex" in said, said[:300])
    from hb.contract import doors as D
    import contextlib
    import io
    doored = _json.loads(_json.dumps(doc))
    doored["rules"][1]["doors"] = ["sales", "brief"]
    said = " | ".join(R.check(doored, root))
    check("catalog: with no venue.states a rule that names doors is refused, naming where doors are kept",
          "names no states catalog (venue.states)" in said, said[:300])
    # the product's states catalog, named under venue.states, is the one list of doors
    catalog = root / "venue" / "states.json"
    write_config(root, venue_block("venue/states.json"))
    write(catalog, _json.dumps({"schema": "venue-states/v1", "doors": [
        {"id": "sales", "kind": "face"}, {"id": "brief", "kind": "verb", "run": "python tools/venue.py brief --name {name}"}]}))
    known, why = D.vocabulary()
    check("catalog: the vocabulary is the catalog's doors, a verb the engine never listed among them",
          why is None and known == ["sales", "brief"], f"{known} {why}")
    doored["rules"][1]["doors"] = ["sales", "brief"]
    check("catalog: a door in the catalog is accepted", R.check(doored, root) == [], str(R.check(doored, root)))
    doored["rules"][1]["doors"] = ["sales", "org --bare"]
    said = " | ".join(R.check(doored, root))
    check("catalog: a door not in the catalog is refused, naming what it holds and where a door is added",
          "R-RL-1: door 'org --bare' is not a venue door; the venue knows: sales, brief; "
          "a door is added to venue/states.json with the work that builds it" in said, said[:400])
    write(catalog, "{not json")
    heard = io.StringIO()
    refused = False
    with contextlib.redirect_stdout(heard):
        try:
            R.check(doored, root)
        except SystemExit as ex:
            refused = ex.code == 2
    check("catalog: a catalog that cannot be read is a refusal naming venue.states, never a silent empty list",
          refused and "venue.states names venue/states.json and it cannot be read" in heard.getvalue(), heard.getvalue()[:300])
    write(catalog, _json.dumps({"doors": []}))
    check("catalog: a catalog naming no doors is a problem", "names no doors" in " | ".join(R.check(doored, root)))
    write(catalog, _json.dumps({"schema": "venue-states/v1", "doors": [
        {"id": "sales", "kind": "face"}, {"id": "failing-labor", "kind": "face"}]}))
    required = dict(_json.loads(_json.dumps(doc)), doors_required=True)
    said = " | ".join(R.check(required, root))
    check("doors: with doors_required an eyes rule with no doors is refused; a test rule needs none",
          "R-RL-1: proof eyes names no `doors`" in said and "R-CB-7" not in said, said[:300])
    check("doors: without the flag an older file with no doors is unaffected", R.check(doc, root) == [])
    write(root / "docs" / "manifests" / "r.json", _json.dumps(dict(required, studio={"branch": "studio/r", "commit": "a1b2c3d4"})))
    c12 = C.resolve("#12", root)
    check("doors: the resolver stops on the missing door, naming the rule",
          any("R-RL-1" in p and "doors" in p for p in c12["problems"]), str(c12["problems"])[:300])
    required["rules"][1]["doors"] = ["failing-labor"]
    write(root / "docs" / "manifests" / "r.json", _json.dumps(dict(required, studio={"branch": "studio/r", "commit": "a1b2c3d4"})))
    c13 = C.resolve("#12", root)
    got = {it["item"]: it for it in c13["items"]}
    check("doors: each item carries its doors and the contract its studio",
          not c13["problems"] and got["R-RL-1"]["doors"] == ["failing-labor"]
          and c13.get("studio") == {"branch": "studio/r", "commit": "a1b2c3d4"} and "doors: failing-labor" in C.render(c13),
          str(c13)[:400])
    from hb.runner import seating
    vf = seating.facts_common(c13)
    check("doors: the tester's facts list each item's doors beside the item",
          vf.get("Venue states each item needs, worn before it is driven") == {"R-RL-1": ["failing-labor"]}, str(vf.keys()))

    # 13. the rehearsal builds, checks and undoes each named state through the venue's state commands
    from hb.contract import rehearsal
    write(root / "venue" / "v.py", "import json, sys\n"
          "open('venue/calls.txt', 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
          "if sys.argv[1] == 'capabilities':\n    print(json.dumps(['states'])); sys.exit(0)\n"
          "sys.exit(1 if sys.argv[1:3] == ['check', 'never'] else 0)\n")
    say = lambda verb: f"{PY} venue/v.py {verb}"
    write_config(root, {"capabilities": say("capabilities"), "up": say("up"), "down": say("down"), "list": say("list"),
                        "state_build": say("build {state} --name {name}"), "state_check": say("check {state} --name {name}"),
                        "state_undo": say("undo {state} --name {name}"), "states": "venue/states.json"})
    write(catalog, _json.dumps({"states": [{"id": "reached", "check": "it holds"}, {"id": "never", "check": "it holds"},
                                           {"id": "plain"}, {"id": "outside", "external": True, "says": "a person's step"}]}))

    class Journal:
        events = []

        def append(self, kind, **facts):
            self.events.append(dict(facts, kind=kind))
    walked_contract = {"items": [{"item": "R-1", "states": ["reached", "never", "plain", "outside", "ghost", "all"]}]}
    j = Journal()
    got = rehearsal.walk(j, root, walked_contract, {"venue": "v-1", "name": "venue-1-1"})
    verdicts = {r["state"]: r["verdict"] for r in got["states"]}
    check("rehearsal: each state is graded by the venue's own build and check",
          verdicts == {"reached": "reachable", "never": "unreachable", "plain": "no-predicate", "outside": "external",
                       "ghost": "no-door"} and got["missing"] == ["never"], str(got)[:400])
    calls = (root / "venue" / "calls.txt").read_text(encoding="utf-8").splitlines()
    check("rehearsal: a reachable state is built, checked, undone, built and checked again, by the venue's name",
          [c for c in calls if " reached " in c] == ["build reached --name venue-1-1", "check reached --name venue-1-1",
                                                     "undo reached --name venue-1-1", "build reached --name venue-1-1",
                                                     "check reached --name venue-1-1"], str(calls))
    check("rehearsal: an external state and a state outside the catalog run nothing",
          not [c for c in calls if " outside " in c or " ghost " in c], str(calls))
    check("rehearsal: the journal names the venue it walked", j.events[-1].get("venue") == "venue-1-1", str(j.events[-1])[:200])
    write_config(root, dict(venue_block("venue/states.json"), capabilities=f"{PY} venue/v.py nothing"))
    write(root / "venue" / "v.py", "import sys\nif sys.argv[1] == 'nothing':\n    print('[]')\n")
    j2 = Journal()
    j2.events = []
    got = rehearsal.walk(j2, root, walked_contract, {"venue": "v-1", "name": "venue-1-1"})
    check("rehearsal: a venue without the states capability walks nothing and says not offered",
          got["states"] == [] and j2.events[-1].get("why") == "not offered: states", str(j2.events)[:300])
    # a fixture recipe's `recipe: <name>` step names a state the venue's catalog carries
    named = dict(base, fixture_recipes=[{"id": "r1", "steps": ["recipe: reached"]}, {"id": "r2", "steps": ["recipe: ghost"]}])
    said = " ".join(str(x) for x in _manifest_schema_check(dict(named, items=[item]))[0])
    check("manifest: a recipe step naming a state the catalog lacks is a problem, one it carries is not",
          "names `recipe: ghost`, which the venue states catalog does not carry" in said and "recipe: reached" not in said, said[:300])

    for p in PASS:
        print("PASS", p)
    for f in FAIL:
        print("FAIL", f)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    print(f"TRUE_EXIT={1 if FAIL else 0}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
