from ledger.money import to_cents


def parse_csv(text):
    """'date,amount' header, then one 'YYYY-MM-DD,12.34' row per line -> [(date, cents)]."""
    rows = []
    for line in text.split("\n")[1:]:
        if not line.strip():
            continue
        date, amount = line.split(",")
        rows.append((date.strip(), to_cents(amount.strip())))
    return rows
