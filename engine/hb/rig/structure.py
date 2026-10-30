"""hb.rig.structure - the shape of the engine, checked: seven foundation folders and one arrow.

Every module under engine/hb/ lives in exactly one of the seven foundations. Imports flow one
way: beats may import anything; runner, contract, venue, gates and record may import each
other and rig; rig imports only rig. Every import counts, at module level or inside a
function, because a cycle hidden in a function body is still a cycle. A file outside the seven
or an edge against the arrow is a refusal naming it, so a new module costs a decision about
its foundation instead of landing where the hand was.

    python engine/hb.py rig structure [--root <engine dir>]
"""
import ast
import json
import sys
from pathlib import Path

FOUNDATIONS = ("beats", "runner", "contract", "venue", "gates", "record", "rig")
MIDDLE = {"runner", "contract", "venue", "gates", "record"}
# the files allowed beside the seven folders
LOOSE = {"__init__.py"}


def _imports(path):
    """Every hb module an engine file imports, as 'foundation.module' or 'foundation', with the line."""
    out = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "hb":
            out.extend((a.name, node.lineno) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("hb."):
            parts = node.module.split(".")
            if len(parts) >= 2:
                out.append((parts[1], node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.level:
            out.append(("<relative>", node.lineno))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("hb."):
                    out.append((a.name.split(".")[1], node.lineno))
    return out


def allowed(src, dst):
    if src == "beats":
        return True
    if src in MIDDLE:
        return dst in MIDDLE or dst == "rig"
    return dst == "rig"


def check(hb_dir: Path):
    """(problems) over the package at hb_dir. Empty means the shape holds."""
    problems = []
    for p in hb_dir.iterdir():
        if p.name == "__pycache__":
            continue
        if p.is_dir() and p.name not in FOUNDATIONS:
            problems.append(f"folder outside the seven: {p.relative_to(hb_dir).as_posix()}")
        elif p.is_file() and p.name not in LOOSE:
            problems.append(f"file outside the seven: {p.relative_to(hb_dir).as_posix()}")
    for f in FOUNDATIONS:
        folder = hb_dir / f
        if not folder.is_dir():
            problems.append(f"missing foundation folder: {f}")
            continue
        for p in sorted(folder.glob("*.py")):
            for dst, line in _imports(p):
                rel = p.relative_to(hb_dir).as_posix()
                if dst == "<relative>":
                    problems.append(f"relative import at {rel}:{line}; every import is absolute (hb.<foundation>.<module>)")
                elif dst not in FOUNDATIONS:
                    problems.append(f"import of an unknown foundation at {rel}:{line}: hb.{dst}")
                elif not allowed(f, dst):
                    problems.append(f"import against the arrow at {rel}:{line}: {f} -> {dst}")
        for p in folder.iterdir():
            if p.is_dir() and p.name != "__pycache__":
                problems.append(f"nested folder inside a foundation: {p.relative_to(hb_dir).as_posix()}")
    return problems


def cmd_rig_structure(a):
    root = Path(getattr(a, "root", None) or Path(__file__).resolve().parents[1])
    problems = check(root)
    print(json.dumps({"verdict": "holds" if not problems else "broken", "problems": problems,
                      "foundations": list(FOUNDATIONS), "ok": not problems}, indent=1))
    print(f"TRUE_EXIT={0 if not problems else 1}")
    sys.exit(0 if not problems else 1)


if __name__ == "__main__":
    import sys
    raise SystemExit(cmd_rig_structure(type("A", (), {"root": sys.argv[1] if len(sys.argv) > 1 else None})()))
