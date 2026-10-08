#!/usr/bin/env python3
"""Command-line entry point: ``python search.py --query "..."``.

The engine lives in ``wsp_core/``; this file keeps the documented CLI path.
"""

from wsp_core.search import main

if __name__ == "__main__":
    main()
