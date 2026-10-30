"""hb.run.launch - the card a run opens with.

What will run, said once before any seat starts: how many items, how each is proven,
whether a stand-in and the real model are needed, and the flags the run was started with
(so a later pass can be started the same way). The run goes ahead on its own; with
`launch.hold_s` configured it waits that long first, and a `pause` in the control file
during the wait stops it before anything is spent.
"""
import time
from collections import Counter

from hb.runner.schemas import stop_kinds
from hb.record import control
from hb.contract import policy
from hb.runner import reader

FLAGS = (("no_venue", "--no-venue"), ("no_before", "--no-before"), ("real_llm", "--real-llm"), ("shadow", "--shadow"),
         ("keep_venue", "--keep-venue"))


def flags_of(a):
    """The flags this run was started with, as a later pass would be given them."""
    out = [flag for name, flag in FLAGS if getattr(a, name, False)]
    for name, flag in (("wave", "--wave"), ("runtime", "--runtime")):
        if getattr(a, name, None):
            out += [flag, str(getattr(a, name))]
    return out


def announce(j, a, c, contract, slices=None):
    """Journal the launch card once per run, then hold if the config asks for it."""
    if any(e.get("kind") == "launch" for e in j.events()):
        return
    items = contract.get("items") or []
    proofs = Counter(it.get("proof") or "eyes" for it in items)
    j.append("launch", card={
        "items": len(items), "proofs": dict(proofs), "slices": slices,
        "stand_in": not getattr(a, "no_venue", False),
        "real_model": bool(getattr(a, "real_llm", False)) or policy.needs_real_model(items),
        "second_reader": reader.wanted(a), "stop_kinds": list(stop_kinds()), "flags": flags_of(a)})
    hold = int((c.get("launch") or {}).get("hold_s") or 0)
    until = time.time() + hold
    while time.time() < until:
        if control.pause_requested(j.dir):
            j.append("control", verb="pause", before="launch")
            raise control.Paused(j)
        time.sleep(2)
