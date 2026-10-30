"""hb.rig.words - the words that may not appear in the engine, because each is a product's.

A product fact in the engine is a second source of truth: the product's config says one thing
and a literal says another, and nobody notices which one is live. The product lists its words
once, in product/words.json, and the check refuses any engine line that carries one. The product
folder itself is skipped; a tree with no list has nothing to refuse and says so.

    python engine/hb.py rig words [--root <repo>] [--include product]
"""
import json
import re
import sys
from pathlib import Path

# the product's words live in product/words.json (regex fragments; a bare word matches as a whole
# word, case-insensitive), beside the product and outside the engine; the public cut ships no list
WORDS_FILE = Path(__file__).resolve().parents[3] / "product" / "words.json"


def words():
    if not WORDS_FILE.exists():
        return ()
    return tuple(str(x) for x in json.loads(WORDS_FILE.read_text(encoding="utf-8")))


def pattern(ws=None):
    ws = words() if ws is None else ws
    return re.compile(r"(?<![\w-])(?:" + "|".join(ws) + r")(?![\w-])", re.I) if ws else None


PATTERN = pattern()
# what the check walks: every text file under these folders of the tree
SCOPE = ("engine", "commands", "roles", "templates", "docs", "drills", "examples", "hooks", "README.md", "AGENTS.md", "LICENSE", ".claude-plugin")
TEXT_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".html", ".sh", ".txt", ".toml", ".cfg", ""}


def _files(root: Path, include_product=False):
    for top in SCOPE:
        if top == "product" and not include_product:
            continue
        p = root / top
        if p.is_file():
            yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES and not (set(f.parts) & {"__pycache__", ".git"}):
                    yield f
    if include_product and (root / "product").is_dir():
        for f in sorted((root / "product").rglob("*")):
            if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES and "__pycache__" not in f.parts:
                yield f


def scan(root: Path, include_product=False):
    """[(path, line_no, word, line)] for every hit outside this file."""
    hits = []
    if PATTERN is None:
        return hits
    me = Path(__file__).resolve()
    for f in _files(root, include_product):
        if f.resolve() == me:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as ex:
            hits.append((f.relative_to(root).as_posix(), 0, "unreadable", f"not UTF-8 text, so not checked: {ex}"))
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in PATTERN.finditer(line):
                hits.append((f.relative_to(root).as_posix(), n, m.group(0), line.strip()[:120]))
    return hits


def cmd_rig_words(a):
    root = Path(getattr(a, "root", None) or Path(__file__).resolve().parents[3])
    hits = scan(root, include_product=bool(getattr(a, "include_product", False)))
    by_word = {}
    for _p, _n, w, _l in hits:
        by_word[w.lower()] = by_word.get(w.lower(), 0) + 1
    for p, n, w, line in hits:
        print(f"{p}:{n}: {w}: {line}")
    verdict = "no-word-list" if PATTERN is None else ("clean" if not hits else "product-words")
    print(json.dumps({"verdict": verdict, "words_file": str(WORDS_FILE) if WORDS_FILE.exists() else None, "hits": len(hits),
                      "files": len({h[0] for h in hits}), "by_word": dict(sorted(by_word.items(), key=lambda kv: -kv[1])),
                      "ok": not hits}, indent=1))
    print(f"TRUE_EXIT={0 if not hits else 1}")
    sys.exit(0 if not hits else 1)
