#!/usr/bin/env python3
"""PASS/FAIL table: is this deployment ready for `CADRE_ENGINE=only`?

    scripts/engine_preflight.py [--data-dir DIR] [--no-seed] [--skip-tools]

Exits nonzero on any FAIL. The checks live in runnerlib/preflight.py — this is
the operator's entry point, because a deploy gate should be a script you can
name, not a module path you have to remember.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runnerlib import preflight

if __name__ == "__main__":
    sys.exit(preflight.main())
