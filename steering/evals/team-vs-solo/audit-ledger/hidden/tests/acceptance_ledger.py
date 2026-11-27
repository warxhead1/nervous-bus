import pytest

from ledger.accounts import Account, InsufficientFunds, transfer
from ledger.importer import parse_csv
from ledger.money import to_cents
from ledger.report import monthly_totals


def test_to_cents_exact():
    assert [to_cents(s) for s in ["0.29", "12.34", "-1.5", "1.00", "19.99"]] == [29, 1234, -150, 100, 1999]


def test_withdraw_full_balance():
    a = Account("a", 100)
    a.withdraw(100)
    assert a.balance == 0
    with pytest.raises(InsufficientFunds):
        a.withdraw(1)


def test_transfer_atomic():
    src, dst = Account("s", 50), Account("d", 0)
    with pytest.raises(InsufficientFunds):
        transfer(src, dst, 80)
    assert (src.balance, dst.balance) == (50, 0)


def test_monthly_totals_split_by_month():
    t = monthly_totals([("2026-01-15", 100), ("2026-02-01", 50), ("2026-01-31", 25)])
    assert t == {"2026-01": 125, "2026-02": 50}


def test_parse_csv_keeps_last_row():
    assert parse_csv("date,amount\n2026-01-01,1.00\n2026-01-02,2.50") == [("2026-01-01", 100), ("2026-01-02", 250)]
    assert parse_csv("date,amount\n2026-01-01,1.00\n") == [("2026-01-01", 100)]
