#!/usr/bin/env python
"""hb - the entry every caller invokes: `python .../engine/hb.py <verb>`.

The engine itself is the package beside this file, one module per seam of the
work it does; `engine/hb/__init__.py` carries the verb list and the contract.
This file stays a file so that path, and the paths hb resolves against it,
never move.
"""
import hb

if __name__ == "__main__":
    hb.main()
