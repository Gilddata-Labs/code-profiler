#!/usr/bin/env python3
"""GildData Codebase Profiler. Run: python gilddata_profiler.py  (guided)
or: python gilddata_profiler.py --github-org ACME   (see --help)."""
import sys

if sys.version_info < (3, 9):
    sys.exit("Python 3.9 or newer is needed.")

from profiler.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
