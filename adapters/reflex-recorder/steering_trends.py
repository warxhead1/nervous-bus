"""steering_trends.py — read side of the steering_hits and stretch_opens detectors.

Reads the aggregate tables the detectors write (steering_hit_*, stretch_open_*)
and returns rows for `reflex steering-hits` and `reflex stretch-opens`. Read
only; never opens the DB itself.

`--change DAY` splits any grouping into `before` (day < DAY) and `after`
(day >= DAY) so a ruling change reads as one comparison. Averages are weighted:
avg_sentence is the sum of per-record averages over scored records, so one
long reply does not outweigh many short ones.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

_TABLE_HINT = "no {t} table yet: the nightly synthesis creates it (run synthesis.py)"


def _period(change: Optional[str]) -> str:
    return f"CASE WHEN day < '{change}' THEN 'before' ELSE 'after' END" if change else "''"


def _require(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _check_day(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if len(value) != 10 or value[4] != "-" or value[7] != "-" or not value.replace("-", "").isdigit():
        raise ValueError(f"expected YYYY-MM-DD, got {value!r}")
    return value


def steering_hit_rows(
    conn: sqlite3.Connection, by: str = "day", since: Optional[str] = None,
    change: Optional[str] = None, project: Optional[str] = None,
    plugin: Optional[str] = None, check: Optional[str] = None,
) -> list[dict]:
    since, change = _check_day(since), _check_day(change)
    if not _require(conn, "steering_hit_daily"):
        raise LookupError(_TABLE_HINT.format(t="steering_hit_daily"))
    where, params = ["1=1"], []
    for col, val in (("project", project), ("plugin", plugin), ("check_name", check)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    if since:
        where.append("day >= ?")
        params.append(since)
    w = " AND ".join(where)
    per = _period(change)

    if by == "rule":
        rows = conn.execute(
            f"SELECT {per} AS period, check_name AS \"check\", rule, SUM(n) AS findings, "
            f"COUNT(DISTINCT day) AS days FROM steering_hit_rules WHERE {w} "
            f"GROUP BY period, check_name, rule ORDER BY period DESC, findings DESC", params
        ).fetchall()
        return [_clean(dict(zip(("period", "check", "rule", "findings", "days"), r))) for r in rows]

    group = {"day": "day", "session": "day, session_id", "project": "project",
             "check": "check_name"}.get(by)
    if group is None:
        raise ValueError(f"unknown grouping {by!r}")
    rows = conn.execute(
        f"SELECT {per} AS period, {group}, SUM(records), SUM(findings), SUM(blocks), "
        f"SUM(scored), SUM(avg_sentence_sum), MAX(max_sentence), SUM(words), "
        f"SUM(lost_numbers+lost_urls+lost_code+lost_flags+lost_quotes+lost_hedges) "
        f"FROM steering_hit_daily WHERE {w} GROUP BY period, {group} ORDER BY period DESC, {group}",
        params,
    ).fetchall()
    keys = ["period"] + [g.strip() for g in group.split(",")] + [
        "records", "findings", "blocks", "scored", "avg_sum", "max_sentence", "words", "lost"]
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        scored = d.pop("scored")
        asum = d.pop("avg_sum")
        d["findings_per_record"] = round(d["findings"] / d["records"], 2) if d["records"] else 0
        d["avg_sentence"] = round(asum / scored, 1) if scored else ""
        out.append(_clean(d))
    return out


def stretch_open_rows(
    conn: sqlite3.Connection, by: str = "day", since: Optional[str] = None,
    change: Optional[str] = None, project: Optional[str] = None,
) -> list[dict]:
    since, change = _check_day(since), _check_day(change)
    if not _require(conn, "stretch_open_daily"):
        raise LookupError(_TABLE_HINT.format(t="stretch_open_daily"))
    where, params = ["1=1"], []
    if project:
        where.append("project = ?")
        params.append(project)
    if since:
        where.append("day >= ?")
        params.append(since)
    w = " AND ".join(where)
    per = _period(change)

    if by == "form":
        rows = conn.execute(
            f"SELECT {per} AS period, kind, form_hash, MAX(length), SUM(n) FROM stretch_open_forms "
            f"WHERE {w} GROUP BY period, kind, form_hash ORDER BY period DESC, SUM(n) DESC LIMIT 50",
            params,
        ).fetchall()
        return [_clean(dict(zip(("period", "kind", "form", "length", "n"), r))) for r in rows]

    group = {"day": "day", "project": "project"}.get(by)
    if group is None:
        raise ValueError(f"unknown grouping {by!r}")
    rows = conn.execute(
        f"SELECT {per} AS period, {group}, SUM(turns_no_click), SUM(turns_open), "
        f"SUM(forms_opened), SUM(forms_not_opened) FROM stretch_open_daily WHERE {w} "
        f"GROUP BY period, {group} ORDER BY period DESC, {group}", params
    ).fetchall()
    out = []
    for period, g, no_click, opened, fo, fn in rows:
        turns = no_click + opened
        out.append(_clean({
            "period": period, group: g, "turns": turns, "turns_open": opened,
            "open_rate": round(opened / turns, 3) if turns else 0,
            "forms_opened": fo, "forms_not_opened": fn,
        }))
    return out


def _clean(row: dict) -> dict:
    """Drop the empty period column when no --change split was asked for."""
    if row.get("period") == "":
        row.pop("period")
    return row
