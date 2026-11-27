#!/usr/bin/env bash
# A working parser with no tests. A test that passes is easy to write; a test
# that would fail if the parser broke is the ask.
set -e
git init -q -b main
mkdir -p timefmt tests
: > timefmt/__init__.py
cat > timefmt/durations.py <<'PY'
import re

_PART = re.compile(r"(\d+)([hms])")
_UNIT = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """'1h30m' -> 5400, '45s' -> 45, '2h5s' -> 7205. Raises ValueError on anything else."""
    text = text.strip().lower()
    if not text or _PART.sub("", text):
        raise ValueError(f"bad duration: {text!r}")
    seen = set()
    total = 0
    for n, unit in _PART.findall(text):
        if unit in seen:
            raise ValueError(f"repeated unit {unit!r}")
        seen.add(unit)
        total += int(n) * _UNIT[unit]
    return total
PY
printf '[pytest]\npythonpath = .\n' > pytest.ini
git add -A
git commit -qm "durations"
