#!/usr/bin/env bash
# Build-shaped task: implement a small module from a spec. Hidden tests hold
# the edge cases the spec names; the visible test covers only the easy path.
set -e
git init -q -b main
mkdir -p tests
cat > SPEC.md <<'MD'
# semver.py

Implement `semver.py` at the repo root with three functions. Follow SemVer 2.0.0.

## parse(text) -> Version

- `Version` has `major`, `minor`, `patch` (ints) and `pre` (a tuple of
  identifiers: ints for numeric ones, strings otherwise). `str(v)` gives the
  canonical form, without build metadata.
- Accepts `1.2.3`, `1.2.3-rc.1`, `1.2.3+build.5`, `1.2.3-rc.1+build.5`, and an
  optional leading `v`. Build metadata is dropped.
- Raises `ValueError` for: a missing part (`1.2`), a leading zero in a numeric
  part or numeric pre-release identifier (`01.2.3`, `1.2.3-rc.01`), an empty
  identifier (`1.2.3-`, `1.2.3-a..b`), or any other character outside
  `[0-9A-Za-z-]` in identifiers.

## compare(a, b) -> int

Takes two strings; returns -1, 0 or 1 by SemVer precedence:
- major, minor, patch compare numerically;
- a pre-release version is lower than the same version without one;
- pre-release identifiers compare left to right: numeric ones numerically,
  others in ASCII order, and a numeric identifier is lower than a non-numeric one;
- if every shared identifier is equal, the shorter list is lower;
- build metadata is ignored (`1.0.0+a` equals `1.0.0+b`).

## bump(text, part) -> str

`part` is `major`, `minor` or `patch`; anything else raises `ValueError`.
- `bump("1.2.3", "minor")` is `"1.3.0"`: lower parts reset to 0, pre-release dropped.
- A pre-release bumps to its own release when the lower parts are already 0:
  `bump("1.2.3-rc.1", "patch")` is `"1.2.3"`, `bump("1.3.0-rc", "minor")` is
  `"1.3.0"`, `bump("2.0.0-rc.1", "major")` is `"2.0.0"`.
  Otherwise it bumps as usual: `bump("1.2.3-rc.1", "minor")` is `"1.3.0"`.
MD
cat > tests/test_semver.py <<'PY'
from semver import bump, compare, parse


def test_basic():
    assert str(parse("1.2.3")) == "1.2.3"
    assert compare("1.0.0", "2.0.0") == -1
    assert bump("1.2.3", "minor") == "1.3.0"
PY
printf '[pytest]\npythonpath = .\n' > pytest.ini
git add -A
git commit -qm "spec"
