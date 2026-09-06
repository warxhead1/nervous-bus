#!/usr/bin/env python3
"""viewgen — schema-aware, deterministic generation and preflight of the two
federated views board.py reads (`beads_global.all_issues` /
`beads_global.all_dependencies`).

Why this exists
---------------
`create_view.sql` used to be hand-maintained, with one hand-written arm per
project DB and a comment begging the next human to "move its arm to the
COALESCE form when another DB migrates". That contract failed exactly the way
a comment-enforced contract fails: bd >= 1.2 migrates a project's
`dependencies` table from `depends_on_id` to
`depends_on_issue_id`/`depends_on_wisp_id`/`depends_on_external` lazily, per
DB, whenever the first bd 1.2 client touches it. The 2026-08-31 snapshot named
four DBs as still-OLD; by 2026-09-06 `deer_flow` had migrated too, its arm
still said `depends_on_id`, and dolt failed the WHOLE view -- not just that
arm:

    (1105, "View 'beads_global.all_dependencies' references invalid table(s)
     or column(s) ...")

A single stale arm therefore takes down `blocked_by` for every project at
once, and board.py's run dies with it.

So the arms are no longer hand-written. They are DERIVED from the live
`information_schema` column shape of each roster DB, and `create_view.sql` is
a generated artifact pinned to a recorded fleet state (`PINNED_VARIANTS`).

Design rules (each one is a failure this module is built to make impossible):

  * EXPLICIT ROSTER. The 10 project DBs are named in `ROSTER`, never
    discovered from `SHOW DATABASES`. A new DB appearing on the server must
    not silently join the federated total, and `beads_global` (the
    federation/routing DB, not a work queue) must never be double-counted.
  * NO SILENT OMISSION. If a roster DB, or its `issues`/`dependencies` table,
    is absent from introspection, generation RAISES. Dropping the arm would
    make the view apply cleanly while quietly deleting a whole project's
    issues from "the total board" -- the worst possible outcome, because it
    looks like success.
  * UNKNOWN SHAPE FAILS CLOSED. Variants are matched by exact predicate. A
    shape that is neither `legacy` nor `split` (including a hybrid carrying
    both `depends_on_id` and the split trio, where we cannot know which column
    bd actually writes) raises `UnknownSchemaError`. Guessing an arm produces
    silently wrong edges.
  * SAFE QUOTING. Every identifier is backtick-quoted with backtick doubling;
    every literal is single-quoted with escaping. Identifiers/literals
    containing control characters are rejected outright. `hearth-loom` is a
    real DB name with a hyphen, so quoting is load-bearing, not decorative.
  * COUNTS AND EDGE IDENTITY PRESERVED. Every dependency row survives the
    union, including wisp and external edges: `depends_on_id` is the COALESCE
    of the three split columns so no row goes NULL-and-vanishing, and a new
    `depends_on_kind` column ('issue'|'wisp'|'external'|'unknown') keeps the
    three edge kinds distinguishable instead of conflated. board.py filters
    `depends_on_kind = 'issue'` before joining to `all_issues`, so a wisp id
    that happens to collide with an issue id can never fabricate a blocker.

Nothing in this module writes. `--emit` prints SQL, `--check` diffs the
checked-in snapshot, `--preflight` runs read-only probes. Applying the SQL is
a separate, human-reviewed step.

Usage:
    python3 viewgen.py --emit                 # render SQL from PINNED_VARIANTS
    python3 viewgen.py --emit --live          # render SQL from the live server
    python3 viewgen.py --check                # diff create_view.sql vs PINNED_VARIANTS (offline)
    python3 viewgen.py --preflight            # live: per-arm probes + count parity
    python3 viewgen.py --preflight --json     # same, machine-readable
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

HERE = Path(__file__).resolve().parent
CREATE_VIEW_SQL = HERE / "create_view.sql"

# The federation/routing DB. Holds the views; is NOT a project with a work
# queue, so it is never a union arm (that would double-count every issue).
FEDERATION_DB = "beads_global"

ISSUES_VIEW = "all_issues"
DEPENDENCIES_VIEW = "all_dependencies"


# --------------------------------------------------------------------------
# Errors -- distinct types so callers (and tests) can tell "the fleet drifted"
# from "somebody handed us a database name with a backtick in it".
# --------------------------------------------------------------------------

class ViewGenError(RuntimeError):
    """Base for every fail-closed condition in this module."""


class MissingSourceError(ViewGenError):
    """A roster DB or one of its required tables is absent. Never downgraded
    to 'skip that arm' -- see the NO SILENT OMISSION rule."""


class UnknownSchemaError(ViewGenError):
    """A table's column shape matches no known variant. Fails closed."""


class UnsafeIdentifierError(ViewGenError):
    """An identifier/literal cannot be safely quoted into SQL."""


# --------------------------------------------------------------------------
# Explicit roster
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectDB:
    """One federated arm: the snake_case (or hyphenated) dolt DB name, and the
    kebab-case repo-facing project name board.py surfaces."""
    db: str
    project: str


# Order is the union order and therefore part of the generated artifact's
# determinism. Alphabetical by db name; do not reorder casually.
ROSTER: Tuple[ProjectDB, ...] = (
    ProjectDB("app_to_market", "app-to-market"),
    ProjectDB("biz_worthy", "biz-worthy"),
    ProjectDB("deer_flow", "deer-flow"),
    ProjectDB("hearth", "hearth"),
    ProjectDB("hearth-loom", "hearth-loom"),
    ProjectDB("nervous_bus", "nervous-bus"),
    ProjectDB("sweepers_adventures", "sweepers-adventures"),
    ProjectDB("temple_stuart_accounting", "temple-stuart-accounting"),
    ProjectDB("tengine", "tengine"),
    ProjectDB("unreal_battlebots_gamedev", "unreal-battlebots-gamedev"),
)

ROSTER_DBS: Tuple[str, ...] = tuple(p.db for p in ROSTER)


# --------------------------------------------------------------------------
# Schema variants
# --------------------------------------------------------------------------

ISSUES_TABLE = "issues"
DEPENDENCIES_TABLE = "dependencies"

# Columns all_issues projects, in order. Every roster DB must have all of
# them; `issues` has drifted a lot across bd versions (is_blocked, no_history,
# rig, ... appear in some DBs and not others) but this core set is stable, and
# a DB missing one of them is a real problem, not something to paper over.
ISSUE_COLUMNS: Tuple[str, ...] = (
    "id", "title", "description", "status", "priority",
    "issue_type", "assignee", "created_at", "updated_at", "closed_at", "notes",
)

# Columns every dependencies variant must supply verbatim.
DEP_COMMON_COLUMNS: Tuple[str, ...] = ("issue_id", "type", "created_at")

# bd < 1.2: a single issue-only target column.
DEP_LEGACY_COLUMN = "depends_on_id"
# bd >= 1.2: target split by kind.
DEP_SPLIT_COLUMNS: Tuple[str, ...] = (
    "depends_on_issue_id", "depends_on_wisp_id", "depends_on_external",
)

VARIANT_ISSUES_CORE = "core"
VARIANT_DEPS_LEGACY = "legacy"
VARIANT_DEPS_SPLIT = "split"

# The output shape of all_dependencies. board.py depends on this exact set.
DEPENDENCY_VIEW_COLUMNS: Tuple[str, ...] = (
    "issue_id", "depends_on_id", "type", "created_at", "depends_on_kind",
)
ISSUES_VIEW_COLUMNS: Tuple[str, ...] = ("project",) + ISSUE_COLUMNS


@dataclass(frozen=True)
class FleetVariants:
    """Resolved schema variant per roster DB, for both tables.

    Keys are DB names. Always complete over ROSTER -- construction fails
    rather than yielding a partial map.
    """
    issues: Mapping[str, str]
    dependencies: Mapping[str, str]

    def as_rows(self) -> List[Tuple[str, str, str]]:
        return [(p.db, self.issues[p.db], self.dependencies[p.db]) for p in ROSTER]


# The fleet state `create_view.sql` in this repo is pinned to. This is the
# offline source of truth: `--check` regenerates from it and diffs, so CI can
# catch a hand-edited create_view.sql without a live dolt server.
#
# Measured 2026-09-06 against 127.0.0.1:39502 via information_schema.columns.
# Delta vs the 2026-08-31 comment block in the previous create_view.sql:
# deer_flow migrated legacy -> split (that is exactly what broke the live
# view). Still legacy: app_to_market, temple_stuart_accounting,
# unreal_battlebots_gamedev.
PINNED_AT = "2026-09-06"
PINNED_VARIANTS: Dict[str, str] = {
    "app_to_market": VARIANT_DEPS_LEGACY,
    "biz_worthy": VARIANT_DEPS_SPLIT,
    "deer_flow": VARIANT_DEPS_SPLIT,
    "hearth": VARIANT_DEPS_SPLIT,
    "hearth-loom": VARIANT_DEPS_SPLIT,
    "nervous_bus": VARIANT_DEPS_SPLIT,
    "sweepers_adventures": VARIANT_DEPS_SPLIT,
    "temple_stuart_accounting": VARIANT_DEPS_LEGACY,
    "tengine": VARIANT_DEPS_SPLIT,
    "unreal_battlebots_gamedev": VARIANT_DEPS_LEGACY,
}


def pinned_fleet() -> FleetVariants:
    """FleetVariants for the checked-in snapshot. Raises if PINNED_VARIANTS
    has drifted out of sync with ROSTER (e.g. a project added to one and not
    the other)."""
    missing = [p.db for p in ROSTER if p.db not in PINNED_VARIANTS]
    if missing:
        raise MissingSourceError(
            "PINNED_VARIANTS is missing roster DBs: " + ", ".join(sorted(missing))
        )
    extra = sorted(set(PINNED_VARIANTS) - set(ROSTER_DBS))
    if extra:
        raise MissingSourceError(
            "PINNED_VARIANTS names DBs absent from ROSTER: " + ", ".join(extra)
        )
    for db, variant in PINNED_VARIANTS.items():
        if variant not in (VARIANT_DEPS_LEGACY, VARIANT_DEPS_SPLIT):
            raise UnknownSchemaError(f"{db}.dependencies: unknown pinned variant {variant!r}")
    return FleetVariants(
        issues={p.db: VARIANT_ISSUES_CORE for p in ROSTER},
        dependencies=dict(PINNED_VARIANTS),
    )


# --------------------------------------------------------------------------
# Safe quoting
# --------------------------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def quote_ident(name: str) -> str:
    """Backtick-quote a MySQL/dolt identifier, doubling embedded backticks.

    `hearth-loom` is a real DB name, so unquoted interpolation is not an
    option. Control characters are rejected rather than escaped -- an
    identifier containing one is a bug upstream, and silently accepting it
    would put an unreviewable byte into generated DDL.
    """
    if not isinstance(name, str) or not name:
        raise UnsafeIdentifierError(f"empty or non-string identifier: {name!r}")
    if _CONTROL_RE.search(name):
        raise UnsafeIdentifierError(f"identifier contains control characters: {name!r}")
    return "`" + name.replace("`", "``") + "`"


def quote_literal(value: str) -> str:
    """Single-quote a string literal, escaping quotes and backslashes."""
    if not isinstance(value, str):
        raise UnsafeIdentifierError(f"non-string literal: {value!r}")
    if _CONTROL_RE.search(value):
        raise UnsafeIdentifierError(f"literal contains control characters: {value!r}")
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def qualified(db: str, table: str) -> str:
    return f"{quote_ident(db)}.{quote_ident(table)}"


def db_referenced_in(definition: str, db: str) -> bool:
    """True if a view definition reads from `db`.

    Used to detect a DROPPED arm -- a view regenerated without one project,
    which applies cleanly, errors on nothing, and silently deletes that
    project from the total board.

    Matching is deliberately not a plain substring test for the backticked
    identifier. dolt's SHOW CREATE VIEW returns the view's ORIGINAL text
    verbatim, and the all_issues deployed on 2026-09-06 was written with bare
    unquoted DB names, so a backtick-only test reported all ten arms missing.
    Accept either quoting, anchored on the `<db>.` table qualifier so `hearth`
    cannot match inside `hearth-loom`.
    """
    pattern = re.compile(
        r"(?:`" + re.escape(db) + r"`|(?<![\w$`])" + re.escape(db) + r")\s*\.",
        re.IGNORECASE,
    )
    return bool(pattern.search(definition))


# --------------------------------------------------------------------------
# Variant resolution (pure -- no I/O, so it is fully testable offline)
# --------------------------------------------------------------------------

ColumnMap = Mapping[Tuple[str, str], Set[str]]


def resolve_issues_variant(db: str, columns: Optional[Iterable[str]]) -> str:
    if columns is None:
        raise MissingSourceError(f"{db}.{ISSUES_TABLE}: table not found on the server")
    cols = {c.lower() for c in columns}
    missing = [c for c in ISSUE_COLUMNS if c not in cols]
    if missing:
        raise UnknownSchemaError(
            f"{db}.{ISSUES_TABLE}: unknown schema variant, missing required column(s): "
            + ", ".join(missing)
        )
    return VARIANT_ISSUES_CORE


def resolve_dependencies_variant(db: str, columns: Optional[Iterable[str]]) -> str:
    """Match `dependencies` to a known variant by exact predicate.

    legacy := depends_on_id present AND no split column present
    split  := all three split columns present AND depends_on_id absent

    Anything else -- a hybrid carrying both forms, a partial split, a table
    missing `issue_id`/`type`/`created_at` -- raises. A hybrid is genuinely
    ambiguous: we cannot tell which column the writing bd client populates,
    and picking wrong yields edges that are silently absent rather than
    loudly broken.
    """
    if columns is None:
        raise MissingSourceError(f"{db}.{DEPENDENCIES_TABLE}: table not found on the server")
    cols = {c.lower() for c in columns}

    missing_common = [c for c in DEP_COMMON_COLUMNS if c not in cols]
    if missing_common:
        raise UnknownSchemaError(
            f"{db}.{DEPENDENCIES_TABLE}: unknown schema variant, missing common column(s): "
            + ", ".join(missing_common)
        )

    has_legacy = DEP_LEGACY_COLUMN in cols
    split_present = [c for c in DEP_SPLIT_COLUMNS if c in cols]

    if has_legacy and not split_present:
        return VARIANT_DEPS_LEGACY
    if len(split_present) == len(DEP_SPLIT_COLUMNS) and not has_legacy:
        return VARIANT_DEPS_SPLIT

    raise UnknownSchemaError(
        f"{db}.{DEPENDENCIES_TABLE}: unknown schema variant "
        f"(depends_on_id={'present' if has_legacy else 'absent'}, "
        f"split columns present: {split_present or 'none'}); "
        "refusing to guess an arm -- fail closed"
    )


def resolve_fleet(column_map: ColumnMap, roster: Sequence[ProjectDB] = ROSTER) -> FleetVariants:
    """Resolve every roster DB. Raises on the FIRST unresolvable DB, but the
    message enumerates every problem found so one round trip surfaces the
    whole drift, not just its alphabetically-first symptom."""
    issues: Dict[str, str] = {}
    deps: Dict[str, str] = {}
    problems: List[str] = []

    for p in roster:
        for table, sink, resolver in (
            (ISSUES_TABLE, issues, resolve_issues_variant),
            (DEPENDENCIES_TABLE, deps, resolve_dependencies_variant),
        ):
            try:
                sink[p.db] = resolver(p.db, column_map.get((p.db, table)))
            except ViewGenError as exc:
                problems.append(str(exc))

    if problems:
        # Preserve the most specific error type when every problem is of one
        # kind, so callers can still distinguish "DB vanished" from "schema
        # drifted"; otherwise report as generic drift.
        joined = "\n  - ".join(problems)
        if all("table not found" in p for p in problems):
            raise MissingSourceError(f"federation preflight failed:\n  - {joined}")
        raise UnknownSchemaError(f"federation preflight failed:\n  - {joined}")

    return FleetVariants(issues=issues, dependencies=deps)


# --------------------------------------------------------------------------
# Introspection (I/O)
# --------------------------------------------------------------------------

def introspect_column_map(conn, roster: Sequence[ProjectDB] = ROSTER) -> Dict[Tuple[str, str], Set[str]]:
    """{(db, table) -> set(column names)} for the roster's issues/dependencies.

    Read-only: a single information_schema query. DBs absent from the server
    simply produce no rows, and resolve_fleet() turns that into
    MissingSourceError rather than a dropped arm.
    """
    placeholders = ", ".join(["%s"] * len(roster))
    sql = (
        "SELECT table_schema, table_name, column_name FROM information_schema.columns "
        f"WHERE table_schema IN ({placeholders}) AND table_name IN (%s, %s)"
    )
    params = [p.db for p in roster] + [ISSUES_TABLE, DEPENDENCIES_TABLE]
    cur = conn.cursor()
    cur.execute(sql, params)
    out: Dict[Tuple[str, str], Set[str]] = {}
    for schema, table, column in cur.fetchall():
        out.setdefault((schema, table), set()).add(str(column).lower())
    return out


def introspect_fleet(conn, roster: Sequence[ProjectDB] = ROSTER) -> FleetVariants:
    return resolve_fleet(introspect_column_map(conn, roster), roster)


# --------------------------------------------------------------------------
# Arm rendering
# --------------------------------------------------------------------------

def render_issues_arm(entry: ProjectDB, variant: str, *, first: bool) -> str:
    if variant != VARIANT_ISSUES_CORE:
        raise UnknownSchemaError(f"{entry.db}.{ISSUES_TABLE}: cannot render variant {variant!r}")
    project_literal = quote_literal(entry.project)
    alias = " AS project" if first else ""
    cols = ", ".join(quote_ident(c) for c in ISSUE_COLUMNS)
    return (
        f"SELECT {project_literal}{alias}, {cols}\n"
        f"  FROM {qualified(entry.db, ISSUES_TABLE)}"
    )


def render_dependencies_arm(entry: ProjectDB, variant: str, *, first: bool) -> str:
    """One dependencies arm, normalised to DEPENDENCY_VIEW_COLUMNS.

    Both variants emit the same 5 columns so the UNION is well-formed and
    board.py sees one stable shape regardless of where the fleet is in its
    migration.
    """
    def a(name: str) -> str:
        # UNION ALL takes its result column names from the first arm; alias
        # everywhere anyway so each arm reads unambiguously on its own.
        return f" AS {quote_ident(name)}" if first else ""

    issue_id = quote_ident("issue_id")
    typ = quote_ident("type")
    created = quote_ident("created_at")

    if variant == VARIANT_DEPS_LEGACY:
        col = quote_ident(DEP_LEGACY_COLUMN)
        target = f"{col}{a('depends_on_id')}"
        kind = (
            f"CASE WHEN {col} IS NOT NULL THEN 'issue' ELSE 'unknown' END"
            f"{a('depends_on_kind')}"
        )
    elif variant == VARIANT_DEPS_SPLIT:
        issue_col, wisp_col, ext_col = (quote_ident(c) for c in DEP_SPLIT_COLUMNS)
        target = f"COALESCE({issue_col}, {wisp_col}, {ext_col}){a('depends_on_id')}"
        kind = (
            "CASE"
            f" WHEN {issue_col} IS NOT NULL THEN 'issue'"
            f" WHEN {wisp_col} IS NOT NULL THEN 'wisp'"
            f" WHEN {ext_col} IS NOT NULL THEN 'external'"
            " ELSE 'unknown' END"
            f"{a('depends_on_kind')}"
        )
    else:
        raise UnknownSchemaError(
            f"{entry.db}.{DEPENDENCIES_TABLE}: cannot render variant {variant!r}"
        )

    return (
        f"SELECT {issue_id}{a('issue_id')}, {target},\n"
        f"       {typ}{a('type')}, {created}{a('created_at')},\n"
        f"       {kind}\n"
        f"  FROM {qualified(entry.db, DEPENDENCIES_TABLE)}"
    )


def render_view(view: str, arms: Sequence[str]) -> str:
    if not arms:
        raise MissingSourceError(f"{view}: refusing to render a view with zero arms")
    body = "\nUNION ALL\n".join(arms)
    return f"CREATE OR REPLACE VIEW {qualified(FEDERATION_DB, view)} AS\n{body};\n"


def render_all_issues(fleet: FleetVariants, roster: Sequence[ProjectDB] = ROSTER) -> str:
    arms = [
        render_issues_arm(p, fleet.issues[p.db], first=(i == 0))
        for i, p in enumerate(roster)
    ]
    return render_view(ISSUES_VIEW, arms)


def render_all_dependencies(fleet: FleetVariants, roster: Sequence[ProjectDB] = ROSTER) -> str:
    arms = [
        render_dependencies_arm(p, fleet.dependencies[p.db], first=(i == 0))
        for i, p in enumerate(roster)
    ]
    return render_view(DEPENDENCIES_VIEW, arms)


# --------------------------------------------------------------------------
# Whole-file rendering
# --------------------------------------------------------------------------

_HEADER = """\
-- adapters/board/create_view.sql
--
-- GENERATED FILE -- do not hand-edit.
--   regenerate: python3 adapters/board/viewgen.py --emit > adapters/board/create_view.sql
--   verify:     python3 adapters/board/viewgen.py --check
--
-- Federated "total board" views: one row per issue, and one row per
-- dependency edge, across all {n} project beads DBs on the shared dolt SQL
-- server (127.0.0.1:39502, data_dir /home/eric/.beads/dolt). `{fed}` itself is
-- excluded -- it is the routing/federation DB, not a project with its own work
-- queue -- and it is where the views live, because it is the one federation-
-- aware DB every project already talks to and because writing the object into
-- a single project's DB would bias the "total" toward that project.
--
-- Idempotent: CREATE OR REPLACE, safe to re-run.
--
-- Apply with:
--   dolt --data-dir /home/eric/.beads/dolt sql < adapters/board/create_view.sql
--
-- SCHEMA DRIFT. bd >= 1.2 migrates a project DB's `dependencies` table from
-- `depends_on_id` to `depends_on_issue_id`/`depends_on_wisp_id`/
-- `depends_on_external`. The migration is lazy and per-DB (first bd 1.2 client
-- to touch that DB), so the fleet is mixed for as long as it takes every
-- project to get touched. A single stale arm does not degrade one project --
-- dolt rejects the WHOLE view ("references invalid table(s) or column(s)"),
-- so `blocked_by` dies for everyone at once. That is why these arms are
-- generated from live `information_schema` shape instead of hand-maintained:
-- the 2026-08-31 hand-written snapshot went stale when deer_flow migrated, and
-- the view was broken on 2026-09-06 until regenerated.
--
-- `all_dependencies` normalises both variants to the same 5 columns.
-- `depends_on_id` is the COALESCE of the split trio so no edge is lost, and
-- `depends_on_kind` ('issue'|'wisp'|'external'|'unknown') keeps the three
-- target kinds distinguishable -- board.py joins only `kind = 'issue'` rows to
-- `all_issues`, so a wisp/external id can never masquerade as a blocker.
--
-- Roster: dolt DB name -> repo-facing project name, and the resolved
-- `dependencies` variant pinned at {pinned}:
{roster_block}
"""


def render_create_view_sql(fleet: FleetVariants, roster: Sequence[ProjectDB] = ROSTER,
                           *, pinned_at: str = PINNED_AT) -> str:
    """The full create_view.sql artifact. Deterministic: identical fleet +
    roster always renders byte-identical output."""
    width_db = max(len(p.db) for p in roster)
    width_proj = max(len(p.project) for p in roster)
    lines = []
    for p in roster:
        variant = fleet.dependencies[p.db]
        lines.append(
            f"--   {p.db.ljust(width_db)}  ->  {p.project.ljust(width_proj)}  [{variant}]"
        )
    header = _HEADER.format(
        n=len(roster), fed=FEDERATION_DB, pinned=pinned_at,
        roster_block="\n".join(lines),
    )
    return (
        header
        + "\n"
        + render_all_issues(fleet, roster)
        + "\n"
        + render_all_dependencies(fleet, roster)
    )


def check_snapshot(path: Path = CREATE_VIEW_SQL) -> Tuple[bool, str]:
    """Compare the checked-in artifact against what PINNED_VARIANTS renders.

    Offline -- no dolt server needed -- so CI can catch a hand-edited
    create_view.sql. Returns (ok, unified_diff).
    """
    expected = render_create_view_sql(pinned_fleet())
    try:
        actual = path.read_text()
    except FileNotFoundError:
        return False, f"{path} does not exist"
    if actual == expected:
        return True, ""
    diff = "".join(difflib.unified_diff(
        expected.splitlines(keepends=True), actual.splitlines(keepends=True),
        fromfile="generated (PINNED_VARIANTS)", tofile=str(path),
    ))
    return False, diff


# --------------------------------------------------------------------------
# Preflight (live, read-only)
# --------------------------------------------------------------------------

@dataclass
class ArmResult:
    db: str
    project: str
    table: str
    variant: Optional[str]
    ok: bool
    rows: Optional[int] = None
    error: Optional[str] = None


@dataclass
class PreflightReport:
    ok: bool = True
    arms: List[ArmResult] = field(default_factory=list)
    drift: List[str] = field(default_factory=list)
    parity: Dict[str, dict] = field(default_factory=dict)
    union_dryrun: Dict[str, dict] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "arms": [vars(a) for a in self.arms],
            "drift": self.drift,
            "union_dryrun": self.union_dryrun,
            "parity": self.parity,
            "errors": self.errors,
        }


def _scalar(cur, sql: str) -> int:
    cur.execute(sql)
    row = cur.fetchone()
    return int(row[0])


def view_select_body(sql: str) -> str:
    """Strip `CREATE OR REPLACE VIEW <name> AS` and the trailing `;` off a
    rendered statement, leaving the bare SELECT union."""
    body = re.sub(r"^\s*CREATE OR REPLACE VIEW\s+\S+\s+AS\s*", "", sql, flags=re.IGNORECASE)
    return body.rstrip().rstrip(";")


def dry_run_union(conn, sql: str) -> Tuple[bool, Optional[str]]:
    """Execute the EXACT union the coordinator is about to install, wrapped in
    a `LIMIT 0` subquery, without running any DDL.

    Probing arms individually proves each arm parses; it does NOT prove the
    union is well-formed (column count/type compatibility across arms is a
    separate failure class). This closes that gap read-only, so a review can
    say the statement is valid before anyone types CREATE.
    """
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT * FROM (\n{view_select_body(sql)}\n) AS dry LIMIT 0")
        cur.fetchall()
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def preflight(conn, roster: Sequence[ProjectDB] = ROSTER) -> PreflightReport:
    """Probe EVERY arm individually, then check count parity against the
    deployed views.

    Probing arms one at a time is the whole point: dolt reports a broken
    federated view as one opaque error naming no arm, so the only way to learn
    which project drifted is to run each arm alone. Every arm is probed even
    after one fails, so a single run enumerates all the damage.
    """
    report = PreflightReport()

    try:
        column_map = introspect_column_map(conn, roster)
    except Exception as exc:  # pragma: no cover -- server-level failure
        report.ok = False
        report.errors.append(f"information_schema introspection failed: {exc}")
        return report

    cur = conn.cursor()
    issue_total = 0
    dep_total = 0
    issue_countable = True
    dep_countable = True
    resolved_deps: Dict[str, str] = {}

    for p in roster:
        for table in (ISSUES_TABLE, DEPENDENCIES_TABLE):
            resolver = resolve_issues_variant if table == ISSUES_TABLE else resolve_dependencies_variant
            renderer = render_issues_arm if table == ISSUES_TABLE else render_dependencies_arm
            try:
                variant = resolver(p.db, column_map.get((p.db, table)))
            except ViewGenError as exc:
                report.ok = False
                report.arms.append(ArmResult(p.db, p.project, table, None, False, error=str(exc)))
                if table == ISSUES_TABLE:
                    issue_countable = False
                else:
                    dep_countable = False
                continue

            if table == DEPENDENCIES_TABLE:
                resolved_deps[p.db] = variant

            arm = renderer(p, variant, first=True)
            try:
                # Executes the arm's real projection (CASE/COALESCE included)
                # without materialising it, then counts rows for parity.
                cur.execute(f"SELECT * FROM (\n{arm}\n) AS arm LIMIT 0")
                cur.fetchall()
                rows = _scalar(cur, f"SELECT COUNT(*) FROM {qualified(p.db, table)}")
            except Exception as exc:
                report.ok = False
                report.arms.append(
                    ArmResult(p.db, p.project, table, variant, False, error=f"{type(exc).__name__}: {exc}")
                )
                if table == ISSUES_TABLE:
                    issue_countable = False
                else:
                    dep_countable = False
                continue

            report.arms.append(ArmResult(p.db, p.project, table, variant, True, rows=rows))
            if table == ISSUES_TABLE:
                issue_total += rows
            else:
                dep_total += rows

    # Drift vs the pinned snapshot -- reported, not fatal on its own: it means
    # create_view.sql needs regenerating and re-applying.
    for db, variant in sorted(resolved_deps.items()):
        pinned = PINNED_VARIANTS.get(db)
        if pinned is None:
            report.drift.append(f"{db}: live variant {variant!r} but DB is not in PINNED_VARIANTS")
        elif pinned != variant:
            report.drift.append(f"{db}: pinned {pinned!r} but live is {variant!r}")

    # Dry-run the exact generated union statements (read-only, no DDL).
    # Only meaningful if every arm resolved -- otherwise rendering raises.
    if issue_countable and dep_countable:
        try:
            fleet = FleetVariants(
                issues={p.db: VARIANT_ISSUES_CORE for p in roster},
                dependencies=resolved_deps,
            )
            for view, sql in (
                (ISSUES_VIEW, render_all_issues(fleet, roster)),
                (DEPENDENCIES_VIEW, render_all_dependencies(fleet, roster)),
            ):
                ok, err = dry_run_union(conn, sql)
                report.union_dryrun[view] = {"ok": ok, "error": err}
                if not ok:
                    report.ok = False
        except ViewGenError as exc:
            report.ok = False
            report.errors.append(f"union dry-run could not be rendered: {exc}")
    else:
        report.union_dryrun = {
            v: {"ok": None, "error": "skipped: at least one arm failed to resolve"}
            for v in (ISSUES_VIEW, DEPENDENCIES_VIEW)
        }

    # Count parity against the deployed views. A deployed view that applies
    # cleanly but is missing an arm looks perfectly healthy until you compare
    # its total to the sum of its sources -- so compare.
    for view, total, countable, source in (
        (ISSUES_VIEW, issue_total, issue_countable, ISSUES_TABLE),
        (DEPENDENCIES_VIEW, dep_total, dep_countable, DEPENDENCIES_TABLE),
    ):
        entry: Dict[str, object] = {"source_total": total if countable else None}
        if not countable:
            entry["status"] = "unknown"
            entry["detail"] = f"at least one {source} arm failed; source total is incomplete"
            report.parity[view] = entry
            continue
        try:
            deployed = _scalar(cur, f"SELECT COUNT(*) FROM {qualified(FEDERATION_DB, view)}")
        except Exception as exc:
            entry["status"] = "view_unavailable"
            entry["detail"] = f"{type(exc).__name__}: {exc}"
            entry["deployed_total"] = None
            report.ok = False
            report.parity[view] = entry
            continue
        entry["deployed_total"] = deployed
        if deployed == total:
            entry["status"] = "ok"
        else:
            entry["status"] = "mismatch"
            entry["detail"] = (
                f"deployed view has {deployed} rows, roster sources have {total} "
                f"(delta {deployed - total}) -- an arm is missing, duplicated, or filtered"
            )
            report.ok = False
        report.parity[view] = entry

    return report


def connect(host: str, port: int, user: str, database: str = FEDERATION_DB):
    """pymysql connection, imported lazily so importing this module (and thus
    running the tests) never requires the driver or a live server."""
    import pymysql
    return pymysql.connect(host=host, port=port, user=user, database=database, connect_timeout=10)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _render_preflight_text(report: PreflightReport) -> str:
    out: List[str] = []
    out.append("arm probes:")
    for a in report.arms:
        mark = "ok  " if a.ok else "FAIL"
        rows = f"{a.rows:>7} rows" if a.rows is not None else "      -     "
        out.append(f"  [{mark}] {a.db:<26} {a.table:<13} {str(a.variant or '-'):<7} {rows}")
        if a.error:
            out.append(f"          {a.error}")
    if report.drift:
        out.append("")
        out.append("snapshot drift (create_view.sql needs regenerating + re-applying):")
        out.extend(f"  - {d}" for d in report.drift)
    out.append("")
    out.append("union dry-run (generated statement executed read-only, LIMIT 0, no DDL):")
    for view, d in report.union_dryrun.items():
        state = {True: "ok", False: "FAIL", None: "skipped"}[d.get("ok")]
        out.append(f"  {view}: {state}")
        if d.get("error"):
            out.append(f"    {d['error']}")
    out.append("")
    out.append("count parity (deployed view vs sum of roster sources):")
    for view, p in report.parity.items():
        out.append(f"  {view}: {p.get('status')} deployed={p.get('deployed_total')} sources={p.get('source_total')}")
        if p.get("detail"):
            out.append(f"    {p['detail']}")
    for e in report.errors:
        out.append(f"ERROR: {e}")
    out.append("")
    out.append("RESULT: " + ("PASS" if report.ok else "FAIL"))
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Defaults mirror board.py's so both talk to the same server by default.
    try:
        import board  # noqa
        d_host, d_port, d_user = board.DOLT_HOST, board.DOLT_PORT, board.DOLT_USER
    except Exception:  # pragma: no cover -- board.py import is best-effort
        d_host, d_port, d_user = "127.0.0.1", 39502, "root"

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit", action="store_true", help="print create_view.sql to stdout")
    mode.add_argument("--check", action="store_true", help="diff create_view.sql against PINNED_VARIANTS (offline)")
    mode.add_argument("--preflight", action="store_true", help="live read-only per-arm probe + count parity")
    ap.add_argument("--live", action="store_true", help="with --emit: introspect the live server instead of PINNED_VARIANTS")
    ap.add_argument("--json", action="store_true", help="with --preflight: machine-readable output")
    ap.add_argument("--dolt-host", default=d_host)
    ap.add_argument("--dolt-port", type=int, default=d_port)
    ap.add_argument("--dolt-user", default=d_user)
    args = ap.parse_args(argv)

    if args.emit:
        if args.live:
            conn = connect(args.dolt_host, args.dolt_port, args.dolt_user)
            try:
                fleet = introspect_fleet(conn)
            finally:
                conn.close()
        else:
            fleet = pinned_fleet()
        sys.stdout.write(render_create_view_sql(fleet))
        return 0

    if args.check:
        ok, diff = check_snapshot()
        if ok:
            sys.stdout.write(f"{CREATE_VIEW_SQL} matches PINNED_VARIANTS ({PINNED_AT})\n")
            return 0
        sys.stderr.write(f"{CREATE_VIEW_SQL} is out of date -- regenerate with --emit\n")
        sys.stderr.write(diff)
        return 1

    conn = connect(args.dolt_host, args.dolt_port, args.dolt_user)
    try:
        report = preflight(conn)
    finally:
        conn.close()
    if args.json:
        sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")
    else:
        sys.stdout.write(_render_preflight_text(report) + "\n")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
