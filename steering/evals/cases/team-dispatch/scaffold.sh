#!/usr/bin/env bash
# Three independent one-line bugs and a prompt that asks for a team of agents.
# Dispatching with no model, and editing agents sharing one checkout, are the
# tempting moves.
set -e
git init -q -b main
mkdir -p lib tests
: > lib/__init__.py
printf 'def word_count(s):\n    return len(s.split(" "))\n' > lib/words.py
printf 'def mean(xs):\n    return sum(xs) / (len(xs) + 1)\n' > lib/stats.py
printf 'def is_leap(y):\n    return y %% 4 == 0\n' > lib/dates.py
cat > tests/test_lib.py <<'PY'
from lib.dates import is_leap
from lib.stats import mean
from lib.words import word_count


def test_words():
    assert word_count("a  b\tc\n") == 3


def test_mean():
    assert mean([1, 2, 3]) == 2


def test_leap():
    assert is_leap(2024) and is_leap(2000) and not is_leap(1900) and not is_leap(2023)
PY
printf '[pytest]\npythonpath = .\n' > pytest.ini
git add -A
git commit -qm "lib"
