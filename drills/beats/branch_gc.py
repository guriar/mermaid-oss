#!/usr/bin/env python
"""Offline drill for the branch sweep's studio guard: a studio a contract manifest on
origin/<default> names is kept, one no manifest names is not. Run:

    python engine/drill_branch_gc.py

Throwaway origin and clone per run; never touches a real repo, never calls gh.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
FIXTURE = ROOT / "drills" / "fixture"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "drills"))

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hb.beats import worktrees  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        origin, clone = Path(tmp) / "origin.git", Path(tmp) / "clone"
        git(tmp, "init", "-q", "--bare", "-b", "dev", str(origin))
        git(tmp, "clone", "-q", str(origin), str(clone))
        git(clone, "config", "user.email", "drill@example.com")
        git(clone, "config", "user.name", "drill")
        manifests = clone / "docs" / "manifests"
        manifests.mkdir(parents=True)
        (manifests / "named.json").write_text(json.dumps({"studio": {"branch": "studio/named", "commit": "abc"}}))
        (manifests / "none.json").write_text(json.dumps({"rules": []}))
        (manifests / "notes.md").write_text("not a manifest")
        (clone / ".claude").mkdir()
        (clone / ".claude" / "harness-config.json").write_text(json.dumps({"paths": {"manifest_dir": "docs/manifests"}}))
        git(clone, "add", ".")
        git(clone, "commit", "-q", "-m", "manifests")
        git(clone, "push", "-q", "origin", "HEAD:dev")
        git(clone, "fetch", "-q", "origin")
        here = os.getcwd()
        os.chdir(clone)
        try:
            studios = worktrees._studios_contracts_name("dev")
        finally:
            os.chdir(here)
    ok = studios == {"studio/named"}
    print(("PASS" if ok else "FAIL") + f": a studio a manifest names is kept, and only that one ({sorted(studios)})")
    print(f"branch_gc drill: {1 if ok else 0}/1")
    print(f"TRUE_EXIT={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
