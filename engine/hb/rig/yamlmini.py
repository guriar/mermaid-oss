"""hb.yamlmini - the YAML subset the engine reads, and nothing wider."""

import re

from hb.rig.common import NEWLINE, _YamlSubsetError


def _yaml_strip_comment(line):
    out_chars, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            out_chars.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out_chars.append(ch)
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out_chars.append(ch)
    return "".join(out_chars).rstrip()


def _split_flow(inner):
    """Split a flow sequence body on commas that are not inside quotes."""
    parts, buf, quote = [], [], None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p for p in (x.strip() for x in parts) if p != ""]


def _yaml_scalar(tok, lineno):
    t = (tok or "").strip()
    if t == "":
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    # `[...]` on one line is a flow sequence: empty or a comma-separated list of
    # scalars. Manifests use it for `design_md_suspended: []`, and refusing it
    # made an empty list unexpressible in the one shape YAML makes obvious.
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        if not inner:
            return []
        if "[" in inner or "{" in inner:
            raise _YamlSubsetError(
                f"line {lineno}: nested flow collections are not in this reader's "
                "subset; write the sequence in block form (`- item` per line).")
        return [_yaml_scalar(part, lineno) for part in _split_flow(inner)]
    for bad, what in (("&", "anchor"), ("*", "alias"), ("!", "tag"),
                      ("{", "flow mapping")):
        if t.startswith(bad):
            raise _YamlSubsetError(
                f"line {lineno}: unsupported YAML construct ({what}: {t[:24]!r}). "
                "This reader implements a declared SUBSET and refuses rather than "
                "guessing, rewrite the value as a plain or quoted scalar.")
    low = t.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    return t


def _yaml_load(text):
    """PyYAML if it is importable, else the hand-written subset reader.

    The subset reader is the guaranteed path: hb runs under the host interpreter,
    which carries no PyYAML, and adding a foreign venv to sys.path to borrow one
    would shadow modules for every other command in this file. So PyYAML is used
    only when it is genuinely importable here, and the fallback is not a
    degraded mode: it is the normal one.
    """
    try:
        import yaml            # noqa: PLC0415 - optional, probed at the use site
    except ImportError:
        return _yaml_mini_load(text)
    try:
        return yaml.safe_load(text)
    except Exception:          # noqa: BLE001 - see below
        # MEASURED: a product manifest that is not valid
        # YAML (a line closes a double-quoted scalar early with an inner `"` in
        # `face_surface="draft"`), and PyYAML refuses it while this reader's looser
        # scalar handling accepts it. If PyYAML became importable on this host, the
        # stricter path would flip a manifest that reads fine today back to
        # unparseable and take every gate keyed off it down. So a PyYAML failure
        # falls THROUGH to the subset reader rather than becoming the verdict: the
        # two paths may not disagree in the refusing direction. Fix the manifest
        # quoting and this fallback stops mattering.
        return _yaml_mini_load(text)


def _yaml_mini_load(text):
    """Parse the declared subset. Raises _YamlSubsetError with a line number."""
    rows = []
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        if raw.lstrip().startswith("#"):
            continue
        if raw.strip() in ("---", "..."):
            continue  # single-document only; a SECOND doc is caught below
        lead = raw[:len(raw) - len(raw.lstrip())]
        if "\t" in lead:
            raise _YamlSubsetError(f"line {n}: TAB indentation, YAML forbids it "
                                   "and this reader will not guess a width")
        body = _yaml_strip_comment(raw)
        if not body.strip():
            continue
        rows.append([len(body) - len(body.lstrip()), body.strip(), n])
    if text.count("\n---") > 0:
        raise _YamlSubsetError("multi-document stream ('---' separator), the "
                               "manifest is ONE document by contract")
    if not rows:
        return {}
    val, i = _yaml_block(rows, 0, rows[0][0])
    if i != len(rows):
        raise _YamlSubsetError(f"line {rows[i][2]}: indentation drops below the "
                               "document root, the reader stopped here rather "
                               "than guess which mapping this belongs to")
    return val


def _yaml_block(rows, i, indent):
    if i >= len(rows):
        return None, i
    if rows[i][1].startswith("- ") or rows[i][1] == "-":
        return _yaml_seq(rows, i, indent)
    return _yaml_map(rows, i, indent)


def _yaml_map(rows, i, indent):
    m = {}
    while i < len(rows) and rows[i][0] == indent:
        ind, body, lineno = rows[i]
        if body.startswith("- "):
            raise _YamlSubsetError(f"line {lineno}: a sequence item inside a "
                                   "mapping at the same indent")
        if ":" not in body:
            raise _YamlSubsetError(f"line {lineno}: {body[:40]!r} is neither "
                                   "`key: value` nor a `- ` item")
        key, _, rest = body.partition(":")
        key = key.strip().strip("'\"")
        rest = rest.strip()
        # A block scalar (`|` literal, `>` folded, with any of the -/+ chomping
        # and explicit-indent indicators) owns every following line indented past
        # the key. Manifests use `when_then: |` to hold a multi-line clause, and
        # the whole item list was unreadable for want of it.
        if rest and rest[0] in "|>":
            style = rest[0]
            chomp = "".join(ch for ch in rest[1:] if ch in "-+")
            j, lines = i + 1, []
            while j < len(rows) and rows[j][0] > ind:
                lines.append((rows[j][0], rows[j][1]))
                j += 1
            if lines:
                base = min(w for w, _ in lines)
                body_lines = [(" " * (w - base)) + txt for w, txt in lines]
            else:
                body_lines = []
            if style == "|":
                val = NEWLINE.join(body_lines)
            else:
                val = " ".join(x.strip() for x in body_lines if x.strip())
            if chomp != "-" and body_lines:
                val += NEWLINE
            if chomp == "-":
                val = val.rstrip(NEWLINE)
            m[key] = val
            i = j
            continue
        if rest == "":
            if i + 1 < len(rows) and rows[i + 1][0] > ind:
                child, i = _yaml_block(rows, i + 1, rows[i + 1][0])
                m[key] = child
            else:
                m[key] = None
                i += 1
        else:
            m[key] = _yaml_scalar(rest, lineno)
            i += 1
    return m, i


def _yaml_seq(rows, i, indent):
    seq = []
    while i < len(rows) and rows[i][0] == indent and (
            rows[i][1] == "-" or rows[i][1].startswith("- ")):
        ind, body, lineno = rows[i]
        item = body[1:].strip()
        if item == "":
            if i + 1 < len(rows) and rows[i + 1][0] > ind:
                child, i = _yaml_block(rows, i + 1, rows[i + 1][0])
                seq.append(child)
            else:
                seq.append(None)
                i += 1
            continue
        if ":" in item and not (item[0] in "\"'"):
            # `- key: value` opens a mapping whose remaining keys sit at ind+2.
            sub = list(rows)
            sub[i] = [ind + 2, item, lineno]
            child, i = _yaml_map(sub, i, ind + 2)
            seq.append(child)
            continue
        seq.append(_yaml_scalar(item, lineno))
        i += 1
    return seq, i
