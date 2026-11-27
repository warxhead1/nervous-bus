#!/usr/bin/env bash
# Audit-shaped task: four small modules with five planted bugs, one visible
# test that passes. Hidden tests hold one check per bug.
set -e
git init -q -b main
mkdir -p ledger tests
: > ledger/__init__.py
cat > ledger/money.py <<'PY'
def to_cents(text):
    """'12.34' -> 1234, '0.29' -> 29, '-1.5' -> -150."""
    return int(float(text) * 100)


def fmt(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"
PY
cat > ledger/accounts.py <<'PY'
class InsufficientFunds(Exception):
    pass


class Account:
    def __init__(self, name, balance=0):
        self.name = name
        self.balance = balance

    def deposit(self, cents):
        if cents <= 0:
            raise ValueError("deposit must be positive")
        self.balance += cents

    def withdraw(self, cents):
        """Take cents out; the balance may reach zero but never go below it."""
        if cents <= 0:
            raise ValueError("withdrawal must be positive")
        if cents >= self.balance:
            raise InsufficientFunds(self.name)
        self.balance -= cents


def transfer(src, dst, cents):
    """Move cents from src to dst. On failure neither account changes."""
    dst.deposit(cents)
    src.withdraw(cents)
PY
cat > ledger/report.py <<'PY'
def monthly_totals(entries):
    """entries: [(iso_date 'YYYY-MM-DD', cents)] -> {'YYYY-MM': total_cents}."""
    out = {}
    for date, cents in entries:
        month = date[:6]
        out[month] = out.get(month, 0) + cents
    return out
PY
cat > ledger/importer.py <<'PY'
from ledger.money import to_cents


def parse_csv(text):
    """'date,amount' header, then one 'YYYY-MM-DD,12.34' row per line -> [(date, cents)]."""
    rows = []
    for line in text.split("\n")[1:-1]:
        if not line.strip():
            continue
        date, amount = line.split(",")
        rows.append((date.strip(), to_cents(amount.strip())))
    return rows
PY
cat > tests/test_ledger.py <<'PY'
from ledger.accounts import Account
from ledger.money import fmt


def test_fmt():
    assert fmt(1234) == "12.34"


def test_deposit():
    a = Account("a")
    a.deposit(100)
    assert a.balance == 100
PY
printf '[pytest]\npythonpath = .\n' > pytest.ini
git add -A
git commit -qm "ledger"
