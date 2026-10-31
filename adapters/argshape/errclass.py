"""Turn a failed command's output into structured rejections.

A rejection is the binary itself saying "this token is wrong": an unknown
flag, an unknown subcommand, a missing SQL column or relation. Each carries
the token, the program that complained (when the message names one), and any
suggestion the tool printed itself (clap's "tip: a similar argument exists",
cobra's "Did you mean this?", postgres's HINT). A tool's own suggestion is
the highest-confidence correction there is.

Patterns cover the arg parsers agents meet on Linux: GNU getopt, BSD getopt,
clap, cobra/pflag, Go `flag`, argparse, click/typer, git's parse-options,
ugrep, jq; and postgres, sqlite, duckdb, mysql for the SQL side.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

FLAG_TOK = r"(-{1,2}[A-Za-z0-9][\w.\-]*)"


@dataclass
class Rejection:
    kind: str                 # flag | subcmd | column | relation | missing_arg | dash_value
    token: str
    prog: str | None = None   # program named in the message, if any
    hint: str | None = None   # the tool's own suggested replacement
    choices: tuple[str, ...] = ()
    relation: str | None = None


def _q(s: str) -> str:
    return s.strip().strip("'\"`‘’")


# (kind, regex, token group, hint group or None). Order matters: the more
# specific shapes (with hints) first.
FLAG_PATTERNS = [
    # GNU head/tail obsolete `-N` mixed with other options or several files:
    # tail: option used in invalid context -- 2   ->  `-n 2`
    ("numeric", re.compile(r"option used in invalid context -- (\d+)"), 1, None),
    # A value that starts with `-` parsed as an option: ugrep: invalid option ->|reject
    ("dash_value", re.compile(r"(?:invalid|unknown|unrecognized) option[: ]+['‘`]?(-[^\s'’`]*[^\w\s'’`=.,\-][^\s'’`]*)"), 1, None),
    # clap: error: unexpected argument '--limt' found ... tip: a similar argument exists: '--limit'
    ("flag", re.compile(r"unexpected argument '(-[^']+)' found(?:.*?\n)*?.*?tip: (?:a similar argument exists|to pass '[^']*' as a value)[^']*'(-[^'=]+)", re.I), 1, 2),
    ("flag", re.compile(r"unexpected argument '(-[^'=]+)[^']*' found", re.I), 1, None),
    # click: Error: No such option: --limt Did you mean --limit?
    ("flag", re.compile(r"No such option: " + FLAG_TOK + r"(?:[^\n]*?\n?\s*\(?Possible options?:|[^\n]*?Did you mean) " + FLAG_TOK, re.I), 1, 2),
    ("flag", re.compile(r"No such option: " + FLAG_TOK, re.I), 1, None),
    # cobra/pflag
    ("short", re.compile(r"unknown shorthand flag: '(\w)' in (-\S+)"), 1, None),
    ("flag", re.compile(r"unknown flag: " + FLAG_TOK), 1, None),
    # Go stdlib flag
    ("flag", re.compile(r"flag provided but not defined: " + FLAG_TOK), 1, None),
    # GNU getopt_long
    ("flag", re.compile(r"unrecognized option[: ]+['‘`]?" + FLAG_TOK), 1, None),
    ("short", re.compile(r"invalid option -- ['‘`]?(\w)"), 1, None),
    ("short", re.compile(r"illegal option -- (\w)"), 1, None),
    ("flag", re.compile(r"option ['‘`]" + FLAG_TOK + r"['’`] is ambiguous; possibilities:((?: ['‘`]-[^'’`]+['’`])+)"), 1, None),
    # git parse-options: error: unknown option `limt'  /  unknown switch `x'
    ("flag", re.compile(r"error: unknown option [`'‘]([\w.\-]+)['’]"), 1, None),
    ("short", re.compile(r"error: unknown switch [`'‘](\w)['’]"), 1, None),
    # argparse: error: unrecognized arguments: --limt 5
    ("flag", re.compile(r"error: unrecognized arguments?: " + FLAG_TOK), 1, None),
    # ugrep / misc: invalid option -x, unknown option -x, Unknown option: --x
    ("flag", re.compile(r"(?:invalid|unknown|unrecognized|illegal) (?:option|flag|switch)s?[: ]+['‘`\"]?" + FLAG_TOK, re.I), 1, None),
    # option requires an argument
    ("missing_arg", re.compile(r"option requires an argument -- ['‘`]?(\w)"), 1, None),
    ("missing_arg", re.compile(r"option ['‘`]?" + FLAG_TOK + r"['’`]? requires an argument"), 1, None),
    ("missing_arg", re.compile(r"flag needs an argument: ['‘`]?(-\S+?)['’`]?(?:\s|$)"), 1, None),
]

SUBCMD_PATTERNS = [
    # cobra: Error: unknown command "lsit" for "gh repo"\n\nDid you mean this?\n\tlist
    (re.compile(r'unknown command "([^"]+)" for "([^"]+)"(?:\s*\n\s*Did you mean this\?\s*\n\s*(\S+))?'), 1, 3),
    # clap: error: unrecognized subcommand 'lsit' ... tip: a similar subcommand exists: 'list'
    (re.compile(r"unrecognized subcommand '([^']+)'(?:(?:.*?\n)*?.*?tip: (?:some )?similar subcommands? exists?: '([^']+)')?", re.I), 1, 2),
    # git: git: 'stauts' is not a git command. ... The most similar command is\n\tstatus
    (re.compile(r"'([^']+)' is not a [\w-]+ command\.(?:(?:.*?\n)*?\s*The most similar commands? (?:is|are)\s*\n\s*(\S+))?"), 1, 2),
    # click: Error: No such command 'lsit'.
    (re.compile(r"No such command [`'‘\"]([^'’\"`]+)['’\"`]"), 1, None),
    # docker: docker: 'lsit' is not a docker command.  (covered above)
]

ARGPARSE_CHOICE = re.compile(r"argument ([\w{},/\-]+): invalid choice: '([^']+)' \(choose from ([^)]*)\)")

PROG_PREFIX = re.compile(r"^(?:error: )?([A-Za-z0-9_.+\-]+): ", re.M)

SQL_PATTERNS = [
    # postgres: ERROR:  column "session_date" does not exist ... HINT:  Perhaps you meant to reference the column "t.trade_date".
    ("column", re.compile(r'column "?([\w.]+)"? does not exist(?:(?:.*?\n)*?HINT:\s+Perhaps you meant to reference the column "([^"]+)")?'), 1, 2),
    ("relation", re.compile(r'relation "([\w.]+)" does not exist'), 1, None),
    # sqlite
    ("column", re.compile(r"no such column: ([\w.]+)"), 1, None),
    ("relation", re.compile(r"no such table: ([\w.]+)"), 1, None),
    # duckdb: Referenced column "x" not found in FROM clause! Candidate bindings: "a", "b"
    ("column", re.compile(r'Referenced column "([^"]+)" not found in FROM clause!(?:\s*Candidate bindings: "([^"]+)")?'), 1, 2),
    ("relation", re.compile(r"Table with name (\w+) does not exist"), 1, None),
    # mysql
    ("column", re.compile(r"Unknown column '([^']+)' in"), 1, None),
    ("relation", re.compile(r"Table '[\w]+\.(\w+)' doesn't exist"), 1, None),
]

USAGE_HINT = re.compile(r"(?im)^\s*(usage:|try ['‘`].*--help|for more (help|information),? (run|try))")


def classify(text: str) -> list[Rejection]:
    """All rejections found in `text`, deduplicated by (kind, token)."""
    out: list[Rejection] = []
    seen: set[tuple[str, str]] = set()
    t = text[:20000]

    def add(r: Rejection) -> None:
        key = (r.kind, r.token)
        if key not in seen:
            seen.add(key)
            out.append(r)

    def prog_before(pos: int) -> str | None:
        line_start = t.rfind("\n", 0, pos) + 1
        m = PROG_PREFIX.match(t, line_start)
        if m and m.end() <= pos + 1:
            p = m.group(1)
            if p.lower() not in ("error", "warning", "fatal", "hint", "tip", "usage"):
                return p
        return None

    for kind, rx, tg, hg in FLAG_PATTERNS:
        for m in rx.finditer(t):
            tok = _q(m.group(tg))
            if kind == "flag" and not tok.startswith("-"):
                tok = ("--" if len(tok) > 1 else "-") + tok   # git prints without dashes
            if kind in ("short", "missing_arg", "numeric") and not tok.startswith("-"):
                tok = "-" + tok
            if kind == "numeric":
                hint = "-n " + tok[1:]
            if kind != "numeric":
                hint = _q(m.group(hg)) if hg and m.group(hg) else None
            if any(r.token == tok for r in out):
                continue
            add(Rejection("flag" if kind in ("short", "numeric") else kind, tok, prog_before(m.start()), hint))
    for rx, tg, hg in SUBCMD_PATTERNS:
        for m in rx.finditer(t):
            hint = _q(m.group(hg)) if hg and m.lastindex and m.lastindex >= hg and m.group(hg) else None
            add(Rejection("subcmd", _q(m.group(tg)), prog_before(m.start()), hint))
    for m in ARGPARSE_CHOICE.finditer(t):
        choices = tuple(_q(c) for c in m.group(3).split(","))
        add(Rejection("subcmd", m.group(2), prog_before(m.start()), None, choices))
    for kind, rx, tg, hg in SQL_PATTERNS:
        for m in rx.finditer(t):
            hint = m.group(hg) if hg and m.group(hg) else None
            tok = m.group(tg)
            hint_choices: tuple[str, ...] = ()
            if hint and "," in hint:
                hint_choices = tuple(_q(h) for h in hint.split(","))
                hint = None
            add(Rejection(kind, tok, None, hint, hint_choices))
    return out


def looks_like_usage(text: str) -> bool:
    return bool(USAGE_HINT.search(text[:20000]))


_NORM = [
    (re.compile(r"/[\w.\-/]+"), "<path>"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<hex>"),
    (re.compile(r"\b\d+(\.\d+)*\b"), "<n>"),
    (re.compile(r"(['\"`‘])[^'\"`’\n]{1,80}(['\"`’])"), r"\1<s>\2"),
    (re.compile(r"\s+"), " "),
]


def signature(text: str) -> str:
    """Stable fingerprint of an error: first informative line, normalized."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and lines[0].startswith("Exit code"):
        lines = lines[1:]
    pick = next((ln for ln in lines if re.search(r"error|invalid|unknown|unrecognized|not found|does not exist|no such", ln, re.I)),
                lines[0] if lines else "")
    s = pick.strip()[:200]
    for rx, rep in _NORM:
        s = rx.sub(rep, s)
    return s.strip()


def exit_code(text: str) -> int | None:
    m = re.match(r"Exit code (\d+)", text)
    return int(m.group(1)) if m else None
