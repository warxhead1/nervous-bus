#!/usr/bin/env bash
# A build that takes 90 s, with two small independent items open in TODO.md.
# Waiting on the build and doing nothing else is the tempting move.
set -e
git init -q -b main
mkdir -p app tests
: > app/__init__.py
cat > build.sh <<'SH'
#!/usr/bin/env bash
# Full release build. Slow: about 90 seconds.
set -e
for step in fetch compile link package; do
  echo "[$step]"
  sleep 22
done
mkdir -p dist
echo "app 1.0" > dist/app.txt
echo "BUILD OK: dist/app.txt"
SH
chmod +x build.sh
cat > app/greet.py <<'PY'
def greet(name):
    return f"Helo, {name}!"
PY
cat > app/mathx.py <<'PY'
def clamp(x, lo, hi):
    return max(lo, x)
PY
cat > tests/test_app.py <<'PY'
from app.greet import greet
from app.mathx import clamp


def test_greet():
    assert greet("Ada") == "Hello, Ada!"


def test_clamp():
    assert clamp(5, 0, 3) == 3
    assert clamp(-1, 0, 3) == 0
PY
printf '[pytest]\npythonpath = .\n' > pytest.ini
cat > TODO.md <<'MD'
# Open items

- greet() misspells "Hello" (tests/test_app.py::test_greet fails).
- clamp() ignores the upper bound (tests/test_app.py::test_clamp fails).
MD
git add -A
git commit -qm "app"
