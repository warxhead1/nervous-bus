def monthly_totals(entries):
    """entries: [(iso_date 'YYYY-MM-DD', cents)] -> {'YYYY-MM': total_cents}."""
    out = {}
    for date, cents in entries:
        month = date[:7]
        out[month] = out.get(month, 0) + cents
    return out
