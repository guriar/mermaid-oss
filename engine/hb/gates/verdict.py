"""hb.run.verdict - two facts per result: did it execute validly, and what did the product do.

A gate row or a receipt is read into one verdict: `valid` (the measurement ran to a product
line under no environment fault), `outcome` (WORKS, BROKEN or UNMEASURED), `cause_class`
(product, environment, door or harness), `signature` (the signature that named it) and
`recovery` (the condition an environment fault is rerun once after, or None). An environment
signature is routed to recovery, never to a fix seat; the same signature with a product line
after its recovery is the product's and BROKEN, unless the fault was the host's room (ROOM):
a run that wanted room twice never measured, whatever count its runner printed beside it.

Two sources name a cause, and nothing else greps an artifact or a receipt for one: the
engine's own SIGNATURES (the host's room, a timeout, a lost venue or seat, a missing result)
and the product's `gates.signatures` (its test runner's and its database's faults). An engine
signature reads only what a machine wrote about how a measurement ended: a receipt's
substate, a gate's own unmeasured reason, and an artifact's process lines (a fatal exception
and a traceback's exception-type line). A product signature reads the gate's own reason and
its whole artifact, so it is anchored the way the product writes it; never a tester's
sentence, never a test's name and never an assertion's text, since those describe the product.
"""
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from hb.rig.common import cfg_or_empty


WORKS, BROKEN, UNMEASURED = "WORKS", "BROKEN", "UNMEASURED"
# the recovery condition of a fault that is the host's room, not the product's
ROOM = "room from admission"
# the recovery condition of a fault a fresh test database lease answers
DB_ANSWERS = "the leased database answers"
# a product signature's `recovery` word, read into the condition the runner reruns after
RECOVERY_WORDS = {"room": ROOM, "lease": DB_ANSWERS}
# the lines of an artifact a process wrote about how it ended; an exception line is one that
# starts with the exception's type, so an assertion message or a test id never matches
PROCESS_LINE = re.compile(r"^(?:Windows fatal exception.*"
                          r"|(?:E\s+)?[A-Za-z_][\w.]*(?:Error|Exception|Exit|OutOfMemory)\b(?::.*)?"
                          r"|run-gate: TIMEOUT.*)$")


def _says(pattern):
    rx = re.compile(pattern, re.I | re.M)
    return lambda row, text: bool(rx.search(text))


def _local_date(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone().date()


def _crossed_midnight(row, text):
    """The gate ran across the host's local midnight, where the wall-clock tests go red."""
    s, e = row.get("started"), row.get("ended")
    try:
        return bool(s and e and _local_date(s) != _local_date(e))
    except ValueError:
        return False


# The engine's table. One row: (signature, cause class, matcher over (row, text), recovery
# condition). The host's room is read first, then the product's signatures, then the rest.
# The first match names the row. `recovery` None means no rerun can meet the condition from
# here; the row rides UNMEASURED with its signature and the PR says so.
MEMORY = ("memory", "environment",
          _says(r"^(?:E\s+)?(?:MemoryError\b|[\w.]*OutOfMemory\b|OSError: \[WinError 1450\]|OSError: \[Errno 12\]"
                r"|OSError: cannot send)"),
          ROOM)
SIGNATURES = (
    ("clock-window", "environment", _crossed_midnight, "the clock past the window"),
    ("timeout", "environment", _says(r"^timed out after|^run-gate: TIMEOUT"), ROOM),
    ("battery-runner", "environment", _says(r"^battery-runner$"), "the battery runner answers"),
    ("venue-down", "environment", _says(r"^stack-failed$"), "the venue comes up"),
    ("seat-crash", "environment", _says(r"^verifier-failed$"), "the seat sits again"),
    ("process-crash", "environment", _says(r"^Windows fatal exception"), ROOM),
    ("order-dependent", "environment", _says(r"^order-dependent:"), None),
    # the CI boxes gave no result this round; the line names which of the five ways (hb.run.remote)
    ("remote", "environment", _says(r"^remote gate: "), None),

    ("ci-only", "harness", _says(r"^ci-only$"), None),
    ("no-result", "harness", _says(r"^no TRUE_EXIT line$"), None),
    ("no-battery", "door", _says(r"^no-battery$"), None),
    ("not-driven", "door", _says(r"^not-driven$"), None),
    ("invalidated", "harness", _says(r"^invalidated$|^before-capture$"), None),
)
# signatures that are nobody's red: only CI's runner can measure it
NOT_A_RED = ("ci-only",)


def verdict(valid, outcome, cause_class, signature, recovery=None):
    return {"valid": bool(valid), "outcome": outcome, "cause_class": cause_class, "signature": signature,
            "recovery": recovery}


@lru_cache(maxsize=64)
def _artifact(path, size, mtime):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def artifact_text(row):
    """The gate's artifact, read once per file version."""
    try:
        p = Path(row["artifact"])
        st = p.stat()
        return _artifact(str(p), st.st_size, st.st_mtime)
    except (KeyError, TypeError, OSError):
        return ""


def process_lines(text):
    """The lines of an artifact that its processes wrote about how they ended."""
    return "\n".join(l.rstrip() for l in text.splitlines() if PROCESS_LINE.match(l.rstrip()))


def _own_lines(row):
    own = [row.get("unmeasured"), "ci-only" if row.get("ci_only") else None]
    return "\n".join(str(p) for p in own if p) + "\n"


def text_of(row):
    """What an engine signature may read: a receipt's substate, a gate's unmeasured reason and
    its artifact's process lines. The tester's `observed` and `seen` are never among them."""
    if "state" in row:
        return str(row.get("substate") or "")
    return _own_lines(row) + process_lines(artifact_text(row))


def product_signatures(c=None):
    """The product's signatures from config `gates.signatures`, as engine rows: each
    `{pattern, cause, recovery?}` is an environment fault; `recovery` is `room`, `lease`, any
    other condition the runner reruns once after, or absent (the row rides UNMEASURED)."""
    if c is None:
        try:
            c = cfg_or_empty()
        except SystemExit:  # read outside any repo: no product, so no product signature
            c = {}
    block = c.get("gates")
    rows = []
    for s in (block.get("signatures") if isinstance(block, dict) else None) or []:
        recovery = s.get("recovery")
        rows.append((str(s["cause"]), "environment", _says(str(s["pattern"])),
                     RECOVERY_WORDS.get(recovery, recovery)))
    return rows


def lease_busy(text, c=None):
    """True when a lease's refusal is a product signature a fresh lease answers (a database
    starting up, a template another clone holds): a busy venue to wait out, never a red."""
    return any(recovery == DB_ANSWERS and match({}, str(text or ""))
               for _name, _cls, match, recovery in product_signatures(c))


def product_line(row):
    """True when the gate ran to its end: a gate whose config names an `outcome` pattern
    printed it (a test runner's closing count), any other printed its TRUE_EXIT line. A
    receipt's product line is a grade that is not UNMEASURED."""
    if "state" in row:
        return row.get("state") in (WORKS, BROKEN)
    if row.get("outcome"):
        return bool(re.search(row["outcome"], artifact_text(row), re.M))
    return row.get("true_exit") is not None


def _signature(row):
    text = text_of(row)
    whole = text if "state" in row else _own_lines(row) + artifact_text(row)
    name, cause_class, match, recovery = MEMORY
    if match(row, text):
        return name, cause_class, recovery
    for name, cause_class, match, recovery in product_signatures():
        if match(row, whole):
            return name, cause_class, recovery
    for name, cause_class, match, recovery in SIGNATURES:
        if match(row, text):
            return name, cause_class, recovery
    return None, "product", None


def classify(row, after=None):
    """The verdict of one gate row or receipt. `after` is the verdict of the same measurement
    before its recovery rerun: the same environment signature with a product line then is
    the product's own red, never a second recovery."""
    green = row.get("state") == WORKS if "state" in row else row.get("true_exit") == 0
    if green:
        return verdict(True, WORKS, "product", None)
    if row.get("verdict") and after is None:
        # the measurement's own reading, taken with its recovery context; a later reader keeps it
        return dict(row["verdict"])
    ran = product_line(row)
    name, cause_class, recovery = _signature(row)
    if after and name and name == after.get("signature") and ran and recovery != ROOM:
        return verdict(True, BROKEN, "product", f"{name} recurred after {after.get('recovery')}")

    if cause_class == "environment":
        return verdict(False, UNMEASURED, cause_class, name, recovery)
    if name in NOT_A_RED or cause_class in ("door", "harness"):
        return verdict(cause_class != "harness", UNMEASURED, cause_class, name)
    if not ran:
        # red with no product line and no named fault: a crash, recovered the same way
        return verdict(False, UNMEASURED, "environment", "crash", "room from admission")
    return verdict(True, BROKEN, "product", name)


def is_red(row):
    """A row the PR must name: anything but WORKS that was there to measure."""
    v = classify(row)
    return v["outcome"] != WORKS and v["signature"] not in NOT_A_RED


def is_fixable(row):
    """A red a fix seat can turn green: valid and the product's."""
    v = classify(row)
    return v["valid"] and v["outcome"] == BROKEN


def unmeasured_why(first, again):
    """The one line an UNMEASURED gate carries to the PR after its bounded rerun."""
    return (f"{first['signature']} ({first['cause_class']}): rerun once after {first['recovery']}, "
            f"still {again['signature'] or 'no product line'}")
