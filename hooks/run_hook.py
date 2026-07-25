#!/usr/bin/env python
"""Plugin launcher for the slicegrep PreToolUse hook.

Self-locating on purpose: it adds the bundled ``src/`` next to this file to
sys.path, so installing the plugin requires no pip install and cannot
conflict with whatever slicegrep version the user may already have. If the
import fails for any reason we exit 0 with no output, which defers to the
normal permission flow and lets the plain read happen.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

try:
    from slicegrep.hook import main
except Exception:
    sys.exit(0)          # fail open: never break a session over retrieval

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
