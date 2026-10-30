#!/usr/bin/env python
"""hb - harness bin: the mechanical layer every command and every runner seat calls.

Engine code: no project facts live here; they come from .claude/harness-config.json.
Every subcommand prints ONE JSON object to stdout and exits 0 (ok) / 2 (actionable
failure, the JSON says what) / 3 (misuse). The verbs are listed once, in verbs.py;
`hb --help` renders that table.
"""
import sys

# Windows consoles default to cp1252, which cannot encode the ·/⚠️/→ that recon
# digests and warning flags are full of - every such call died in print(), AFTER
# its side effects had already landed. UTF-8 is the wire format for all output.
# Under pythonw (the page's scheduled task) there is no console: the verb opens its own log.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None:
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _children_open_no_window():
    """Every child the engine starts on Windows gets a hidden console of its own.

    The page runs under pythonw and a detached run has no visible console, so a git
    or gate child with no flags opens a console window that flashes on the owner's
    screen, every refresh. A caller that names its own flags keeps them."""
    import os
    import subprocess

    if os.name != "nt" or getattr(subprocess.Popen, "_hb_no_window", False):
        return
    plain_init = subprocess.Popen.__init__

    def init(self, *args, **kw):
        if not kw.get("creationflags"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        plain_init(self, *args, **kw)

    subprocess.Popen.__init__ = init
    subprocess.Popen._hb_no_window = True


_children_open_no_window()

from hb.beats.cli import main

__all__ = ["main"]
