"""Local conftest for ``pulse_app/tests/`` — sys.path bootstrap.

Ensures the adapter directory is on ``sys.path`` so ``import pulse_app``
works when pytest is invoked directly against this subdirectory.
Mirrors the parent ``tests/conftest.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# adapters/dashboard/autobench-pulse/ — two levels above this file.
ADAPTER_ROOT = Path(__file__).resolve().parents[2]
# Walk up to the autobench-pulse adapter directory and put it on sys.path so
# ``import pulse_app`` resolves the same way the top-level ``tests/`` suite
# does.
HERE = Path(__file__).resolve().parent
ADAPTER_ROOT = HERE.parent.parent  # pulse_app/tests -> pulse_app -> adapter root
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))
