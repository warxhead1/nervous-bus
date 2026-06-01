"""CLI entrypoint for `pulse-app` and `python -m pulse_app`."""

from __future__ import annotations

from .app import main

__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
