"""SQL surface: find the SQL in a command, learn columns and value shapes.

Connector-agnostic. A segment carries SQL if one of its words (or its heredoc
/ here-string stdin) reads as a statement. The connection is identified by
the program plus its connection-ish arguments, never by credentials.

Value shapes: from rows a successful query printed, each column gets a shape
family (mac, uuid, hex, ...) and a variant (separator, case, grouping),
inferred from at most 20 sampled values. Only the shape is stored, never a
value. A literal compared against that column in a later query is checked
against the shape, and for families with a lossless canonical form (MAC,
UUID, hex case) it is rewritten into the column's variant.
"""
from __future__ import annotations

import hashlib
import re

STMT = re.compile(r"^\s*(?:\\set\s.*\n\s*)*(select|with|update|insert|delete|explain|copy|\\copy|\\d\+?)\b", re.I)
CONN_OPTS = {"-d", "--dbname", "-h", "--host", "-p", "--port", "-U",
             "--username", "-D", "--database", "-S", "--socket"}
SQL_ARG_OPTS = {"-c", "--command", "-e", "--execute", "-q", "--query", "-cmd"}
FROM_RE = re.compile(r"\b(?:from|join|update|into)\s+((?:\"?[\w]+\"?\.)?\"?[\w]+\"?)", re.I)
IDENT_RE = re.compile(r"(?<![\w.'\"$])((?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)(?![\w'\"(])")
SQL_WORDS = set("""select from where and or not null is in as on join left right
inner outer full cross group by order having limit offset with distinct case
when then else end asc desc union all exists between like ilike true false count
sum avg min max coalesce cast interval date timestamp now update set insert into
values delete returning over partition row_number lag lead using natural extract
epoch nulls first last filter any array current_date current_timestamp text int
bigint numeric float double precision varchar boolean explain analyze copy to
stdout csv header lateral""".split())


def find_sql(seg) -> list[tuple[str, int, int, bool]]:
    """(sql_text, start, end, is_stdin) for each SQL-bearing slot in a segment."""
    out = []
    words = seg.args
    for i, w in enumerate(words):
        if w.dynamic:
            continue
        prev = words[i - 1].value if i else ""
        val = w.value
        if "=" in val and val.split("=", 1)[0] in ("--command", "--execute", "--query"):
            val = val.split("=", 1)[1]
        if prev in SQL_ARG_OPTS or STMT.match(val):
            if STMT.match(val):
                out.append((val, w.start, w.end, False))
    if seg.stdin_text and STMT.search(seg.stdin_text) and seg.stdin_span:
        out.append((seg.stdin_text, seg.stdin_span[0], seg.stdin_span[1], True))
    return out


def conn_key(seg, cwd: str) -> str:
    """Hash of program + connection args (+ script path for wrappers)."""
    parts = [seg.bin or ""]
    if seg.bin and "/" in seg.bin and not seg.bin.startswith("/"):
        parts.append(cwd)
    words = seg.args
    for i, w in enumerate(words):
        v = w.value
        if v in CONN_OPTS and i + 1 < len(words):
            parts.append(f"{v}={words[i + 1].value}")
        elif v.startswith(("postgres://", "postgresql://", "mysql://")):
            parts.append(re.sub(r"//[^@/]*@", "//", v))   # drop credentials
        elif re.search(r"\.(db|sqlite3?|duckdb)$", v):
            parts.append(v)
    parts += seg.via
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:16]


def relations(sql: str) -> list[str]:
    rels = []
    for m in FROM_RE.finditer(_strip_literals(sql)):
        r = m.group(1).replace('"', "").lower()
        if r not in SQL_WORDS and r not in rels and r != "(":
            rels.append(r)
    return rels


def _strip_literals(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def identifiers(sql: str) -> set[str]:
    body = _strip_literals(sql)
    out = set()
    for m in IDENT_RE.finditer(body):
        ident = m.group(1)
        if ident.lower() not in SQL_WORDS:
            out.add(ident)
    return out


def replace_ident(text: str, bad: str, good: str) -> str:
    """Replace identifier `bad` with `good` outside string literals."""
    pieces = re.split(r"('(?:[^']|'')*')", text)
    rx = re.compile(r"(?<![\w$])" + re.escape(bad) + r"(?![\w(])")
    return "".join(p if p.startswith("'") else rx.sub(good, p) for p in pieces)


# ---------------------------------------------------------------- outputs

def parse_table(stdout: str, sep: str | None = None) -> tuple[list[str], list[list[str]]]:
    """Header + rows from psql aligned/unaligned, sqlite -header, duckdb box output."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        return [], []
    # psql aligned: header, then ----+---- rule
    if re.match(r"^[-+]+$", lines[1].strip()) and "-" in lines[1]:
        hdr = [h.strip() for h in lines[0].split("|")]
        rows = []
        for ln in lines[2:]:
            if re.match(r"^\(\d+ rows?\)$", ln.strip()):
                break
            rows.append([c.strip() for c in ln.split("|")])
        return hdr, [r for r in rows if len(r) == len(hdr)]
    # duckdb box drawing
    if lines[0].startswith(("┌", "+")):
        body = [ln for ln in lines if ln.startswith(("│", "|"))]
        cells = [[c.strip() for c in ln.strip("│|").split("│" if "│" in ln else "|")] for ln in body]
        if len(cells) >= 2:
            hdr = cells[0]
            rows = cells[2:] if len(cells) > 2 and all(re.fullmatch(r"[a-z0-9_ ]*", c) for c in cells[1]) else cells[1:]
            return hdr, [r for r in rows if len(r) == len(hdr)]
    # unaligned: psql -A (sep | by default), sqlite -header (|)
    sep = sep or ("|" if "|" in lines[0] else ("\t" if "\t" in lines[0] else ","))
    hdr = lines[0].split(sep)
    if not all(re.fullmatch(r"[\w?]+", h.strip()) for h in hdr):
        return [], []
    rows = [ln.split(sep) for ln in lines[1:] if not re.match(r"^\(\d+ rows?\)$", ln.strip())]
    return [h.strip() for h in hdr], [r for r in rows if len(r) == len(hdr)]


def output_sep(seg) -> str | None:
    words = [w.value for w in seg.args]
    for i, v in enumerate(words):
        if v in ("-F", "--field-separator", "-separator") and i + 1 < len(words):
            return words[i + 1]
        if v.startswith("--field-separator="):
            return v.split("=", 1)[1]
    return None


# ---------------------------------------------------------------- shapes

HEX = "0-9A-Fa-f"
SHAPES = [
    ("mac", re.compile(rf"^[{HEX}]{{2}}([:\-])[{HEX}]{{2}}(\1[{HEX}]{{2}}){{4}}$")),
    ("mac", re.compile(rf"^[{HEX}]{{4}}\.[{HEX}]{{4}}\.[{HEX}]{{4}}$")),
    ("mac", re.compile(rf"^[{HEX}]{{12}}$")),
    ("uuid", re.compile(rf"^[{HEX}]{{8}}-[{HEX}]{{4}}-[{HEX}]{{4}}-[{HEX}]{{4}}-[{HEX}]{{12}}$")),
    ("uuid", re.compile(rf"^[{HEX}]{{32}}$")),
    ("hex", re.compile(rf"^(0x)?[{HEX}]+$")),
]


def shape_of(v: str) -> tuple[str, str] | None:
    """(family, variant) or None. Variant encodes separator/grouping/case."""
    v = v.strip()
    if not v:
        return None
    for fam, rx in SHAPES:
        if rx.match(v):
            if fam == "hex" and not re.search(r"[A-Fa-f]", v):
                return None       # plain integers are not a hex family
            case = "upper" if v == v.upper() and re.search(r"[A-F]", v) else (
                "lower" if re.search(r"[a-f]", v) else "any")
            if fam == "mac":
                if "." in v:
                    grp = "dot4"
                elif ":" in v:
                    grp = "colon"
                elif "-" in v:
                    grp = "dash"
                else:
                    grp = "bare"
                return fam, f"{grp}/{case}"
            if fam == "uuid":
                return fam, f"{'dashed' if '-' in v else 'bare'}/{case}"
            return fam, f"{'0x' if v.lower().startswith('0x') else 'plain'}/{case}"
    return None


def infer_column_shape(values: list[str]) -> tuple[str, str] | None:
    """A shape only if every non-empty sampled value agrees on it."""
    shapes = {shape_of(v) for v in values if v.strip() and v.strip().upper() != "NULL"}
    if None in shapes:
        return None
    if len(shapes) != 1:
        # case "any" values (all digits) are compatible with either case
        fams = {s[0] for s in shapes}
        grps = {s[1].split("/")[0] for s in shapes}
        cases = {s[1].split("/")[1] for s in shapes} - {"any"}
        if len(fams) == 1 and len(grps) == 1 and len(cases) <= 1:
            return fams.pop(), f"{grps.pop()}/{cases.pop() if cases else 'any'}"
        return None
    return shapes.pop()


def convert(value: str, family: str, variant: str) -> str | None:
    """Re-render `value` into (family, variant); None if not losslessly possible."""
    have = shape_of(value)
    if not have or have[0] != family:
        if family == "mac" and have and have[0] == "hex" and len(value) == 12:
            have = ("mac", "bare/" + have[1].split("/")[1])
        else:
            return None
    if have[1] == variant:
        return None
    grp, case = variant.split("/")
    digits = re.sub(r"[^0-9A-Fa-f]", "", value[2:] if family == "hex" and value.lower().startswith("0x") else value)
    if case == "upper":
        digits = digits.upper()
    elif case == "lower":
        digits = digits.lower()
    if family == "mac":
        if len(digits) != 12:
            return None
        if grp == "colon":
            out = ":".join(digits[i:i + 2] for i in range(0, 12, 2))
        elif grp == "dash":
            out = "-".join(digits[i:i + 2] for i in range(0, 12, 2))
        elif grp == "dot4":
            out = ".".join(digits[i:i + 4] for i in range(0, 12, 4))
        else:
            out = digits
    elif family == "uuid":
        if len(digits) != 32:
            return None
        out = (f"{digits[:8]}-{digits[8:12]}-{digits[12:16]}-{digits[16:20]}-{digits[20:]}"
               if grp == "dashed" else digits)
    else:
        out = ("0x" if grp == "0x" else "") + digits
    return out if out != value else None


CMP_RE = re.compile(
    r"((?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)\s*(?:=|<>|!=|(?:not\s+)?in\s*\()\s*((?:'(?:[^']|'')*'\s*,?\s*)+)", re.I)
LIT_RE = re.compile(r"'((?:[^']|'')*)'")


def literal_comparisons(sql: str) -> list[tuple[str, str]]:
    """(column, literal) pairs for `col = 'x'` / `col in ('a','b')`."""
    out = []
    for m in CMP_RE.finditer(sql):
        col = m.group(1).split(".")[-1].lower()
        if col in SQL_WORDS:
            continue
        for lm in LIT_RE.finditer(m.group(2)):
            out.append((col, lm.group(1)))
    return out
