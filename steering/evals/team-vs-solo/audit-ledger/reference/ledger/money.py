from decimal import Decimal


def to_cents(text):
    """'12.34' -> 1234, '0.29' -> 29, '-1.5' -> -150."""
    return int(Decimal(text) * 100)


def fmt(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"
