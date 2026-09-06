#!/usr/bin/env python3
"""Tests for adapters/board/viewgen.py and board.py's federated-view preflight.

House convention (adapters/board/test_board.py, adapters/ci-watch/test_watch.py):
no live dolt server, no real orca DB, no real Redis. Everything here is either
a pure function, a fake DB-API connection, or -- for the parts where a string
assertion would prove nothing -- the GENERATED SQL executed for real against
in-memory sqlite with the roster DBs ATTACHed. sqlite accepts the same
backtick quoting and schema-qualified names dolt does, so a mixed-fleet union
can be executed and its rows counted offline. That is what makes the
count/edge-preservation claims testable instead of asserted.

Two scenarios drive most of this file, because they are the two that actually
broke production:

  * MIXED FLEET -- bd's legacy->split `dependencies` migration is lazy and
    per-DB, so both variants coexist indefinitely and the union must be
    well-formed across them.
  * SECOND MIGRATION -- one more DB flips while the checked-in view is stale.
    That is precisely what happened when deer_flow migrated after the
    2026-08-31 hand-written snapshot, and dolt failed the WHOLE view, not the
    one stale arm.

Run: python3 -m pytest adapters/board -q
"""

from __future__ import annotations

import pathlib
import sqlite3
import unittest
from typing import Dict, List, Optional, Sequence, Set, Tuple

import board
import viewgen
from viewgen import (
    DEP_SPLIT_COLUMNS,
    ISSUE_COLUMNS,
    MissingSourceError,
    ProjectDB,
    UnknownSchemaError,
    UnsafeIdentifierError,
    VARIANT_DEPS_LEGACY,
    VARIANT_DEPS_SPLIT,
    VARIANT_ISSUES_CORE,
)

LEGACY_DEP_COLUMNS = ("id", "issue_id", "depends_on_id", "type", "created_at", "created_by", "metadata")
SPLIT_DEP_COLUMNS = (
    "id", "issue_id", "type", "created_at", "created_by", "metadata",
) + DEP_SPLIT_COLUMNS

# A superset of ISSUE_COLUMNS with some of the columns that genuinely differ
# across the fleet (is_blocked / rig exist in some DBs and not others), to
# prove variant resolution keys off the required core and not exact equality.
ISSUES_COLUMNS_FULL = tuple(ISSUE_COLUMNS) + ("content_hash", "design", "is_blocked", "rig")


def fleet_column_map(dep_variants: Dict[str, str],
                     roster: Sequence[ProjectDB] = viewgen.ROSTER,
                     ) -> Dict[Tuple[str, str], Set[str]]:
    """Build an information_schema-shaped column map for a whole fleet."""
    out: Dict[Tuple[str, str], Set[str]] = {}
    for p in roster:
        out[(p.db, "issues")] = set(ISSUES_COLUMNS_FULL)
        cols = LEGACY_DEP_COLUMNS if dep_variants[p.db] == VARIANT_DEPS_LEGACY else SPLIT_DEP_COLUMNS
        out[(p.db, "dependencies")] = set(cols)
    return out


PINNED_DEP_VARIANTS = dict(viewgen.PINNED_VARIANTS)


# --------------------------------------------------------------------------
# Safe quoting -- `hearth-loom` is a real DB name, so this is load-bearing
# --------------------------------------------------------------------------

class TestQuoting(unittest.TestCase):
    def test_hyphenated_db_is_backtick_quoted(self):
        self.assertEqual(viewgen.quote_ident("hearth-loom"), "`hearth-loom`")

    def test_embedded_backtick_is_doubled(self):
        self.assertEqual(viewgen.quote_ident("we`ird"), "`we``ird`")

    def test_control_characters_rejected_not_escaped(self):
        # Escaping would put an unreviewable byte into generated DDL.
        with self.assertRaises(UnsafeIdentifierError):
            viewgen.quote_ident("bad\nname")
        with self.assertRaises(UnsafeIdentifierError):
            viewgen.quote_ident("bad\x00name")

    def test_empty_identifier_rejected(self):
        with self.assertRaises(UnsafeIdentifierError):
            viewgen.quote_ident("")

    def test_literal_escapes_quote_and_backslash(self):
        self.assertEqual(viewgen.quote_literal("o'brien"), "'o''brien'")
        self.assertEqual(viewgen.quote_literal("back\\slash"), "'back\\\\slash'")

    def test_literal_rejects_control_characters(self):
        with self.assertRaises(UnsafeIdentifierError):
            viewgen.quote_literal("a\nb")

    def test_project_literal_with_quote_survives_rendering(self):
        entry = ProjectDB("weird_db", "o'brien-co")
        sql = viewgen.render_issues_arm(entry, VARIANT_ISSUES_CORE, first=True)
        self.assertIn("'o''brien-co' AS project", sql)
        self.assertIn("`weird_db`.`issues`", sql)


# --------------------------------------------------------------------------
# Variant resolution -- unknown shapes must fail closed, never be guessed
# --------------------------------------------------------------------------

class TestVariantResolution(unittest.TestCase):
    def test_legacy(self):
        self.assertEqual(
            viewgen.resolve_dependencies_variant("app_to_market", LEGACY_DEP_COLUMNS),
            VARIANT_DEPS_LEGACY,
        )

    def test_split(self):
        self.assertEqual(
            viewgen.resolve_dependencies_variant("deer_flow", SPLIT_DEP_COLUMNS),
            VARIANT_DEPS_SPLIT,
        )

    def test_case_insensitive_columns(self):
        self.assertEqual(
            viewgen.resolve_dependencies_variant("x", [c.upper() for c in SPLIT_DEP_COLUMNS]),
            VARIANT_DEPS_SPLIT,
        )

    def test_hybrid_fails_closed(self):
        # Both forms present: we cannot know which column bd writes, and
        # picking wrong yields silently-missing edges. Refuse.
        hybrid = set(SPLIT_DEP_COLUMNS) | {"depends_on_id"}
        with self.assertRaises(UnknownSchemaError) as ctx:
            viewgen.resolve_dependencies_variant("frankendb", hybrid)
        self.assertIn("frankendb", str(ctx.exception))

    def test_partial_split_fails_closed(self):
        partial = set(SPLIT_DEP_COLUMNS) - {"depends_on_external"}
        with self.assertRaises(UnknownSchemaError):
            viewgen.resolve_dependencies_variant("halfway", partial)

    def test_missing_common_column_fails_closed(self):
        with self.assertRaises(UnknownSchemaError) as ctx:
            viewgen.resolve_dependencies_variant("x", set(SPLIT_DEP_COLUMNS) - {"created_at"})
        self.assertIn("created_at", str(ctx.exception))

    def test_missing_table_is_missing_source_not_unknown_schema(self):
        with self.assertRaises(MissingSourceError):
            viewgen.resolve_dependencies_variant("gone", None)
        with self.assertRaises(MissingSourceError):
            viewgen.resolve_issues_variant("gone", None)

    def test_issues_tolerates_extra_columns_but_not_missing_core(self):
        self.assertEqual(
            viewgen.resolve_issues_variant("x", ISSUES_COLUMNS_FULL), VARIANT_ISSUES_CORE
        )
        with self.assertRaises(UnknownSchemaError) as ctx:
            viewgen.resolve_issues_variant("x", set(ISSUES_COLUMNS_FULL) - {"notes"})
        self.assertIn("notes", str(ctx.exception))


# --------------------------------------------------------------------------
# No silent omission
# --------------------------------------------------------------------------

class TestNoSilentOmission(unittest.TestCase):
    def test_absent_roster_db_raises_and_is_named(self):
        cmap = fleet_column_map(PINNED_DEP_VARIANTS)
        del cmap[("tengine", "issues")]
        del cmap[("tengine", "dependencies")]
        with self.assertRaises(MissingSourceError) as ctx:
            viewgen.resolve_fleet(cmap)
        msg = str(ctx.exception)
        self.assertIn("tengine.issues", msg)
        self.assertIn("tengine.dependencies", msg)

    def test_all_problems_enumerated_not_just_the_first(self):
        """One round trip must surface the whole drift. Reporting only the
        alphabetically-first symptom costs a re-run per broken DB."""
        cmap = fleet_column_map(PINNED_DEP_VARIANTS)
        del cmap[("app_to_market", "dependencies")]
        cmap[("tengine", "dependencies")] = set(SPLIT_DEP_COLUMNS) | {"depends_on_id"}
        with self.assertRaises(UnknownSchemaError) as ctx:
            viewgen.resolve_fleet(cmap)
        msg = str(ctx.exception)
        self.assertIn("app_to_market", msg)
        self.assertIn("tengine", msg)

    def test_view_with_zero_arms_refused(self):
        with self.assertRaises(MissingSourceError):
            viewgen.render_view("all_issues", [])

    def test_federation_db_is_never_a_union_arm(self):
        # beads_global holds the views; including it would double-count.
        self.assertNotIn(viewgen.FEDERATION_DB, viewgen.ROSTER_DBS)
        sql = viewgen.render_create_view_sql(viewgen.pinned_fleet())
        self.assertNotIn("`beads_global`.`issues`", sql)
        self.assertNotIn("`beads_global`.`dependencies`", sql)

    def test_every_roster_db_appears_in_both_views(self):
        fleet = viewgen.pinned_fleet()
        issues_sql = viewgen.render_all_issues(fleet)
        deps_sql = viewgen.render_all_dependencies(fleet)
        for p in viewgen.ROSTER:
            self.assertIn(f"{viewgen.quote_ident(p.db)}.`issues`", issues_sql)
            self.assertIn(f"{viewgen.quote_ident(p.db)}.`dependencies`", deps_sql)


# --------------------------------------------------------------------------
# Determinism + the checked-in snapshot
# --------------------------------------------------------------------------

class TestDeterminismAndSnapshot(unittest.TestCase):
    def test_rendering_is_deterministic(self):
        fleet = viewgen.pinned_fleet()
        self.assertEqual(
            viewgen.render_create_view_sql(fleet),
            viewgen.render_create_view_sql(fleet),
        )

    def test_union_order_follows_roster_order(self):
        sql = viewgen.render_all_issues(viewgen.pinned_fleet())
        positions = [sql.index(viewgen.quote_ident(p.db)) for p in viewgen.ROSTER]
        self.assertEqual(positions, sorted(positions))

    def test_checked_in_create_view_sql_matches_pinned_variants(self):
        """create_view.sql is a generated artifact. A hand-edit -- the exact
        practice that let the 2026-08-31 snapshot rot -- fails here, offline,
        with no dolt server required."""
        ok, diff = viewgen.check_snapshot()
        self.assertTrue(ok, f"create_view.sql is stale; regenerate with --emit\n{diff}")

    def test_pinned_variants_covers_roster_exactly(self):
        self.assertEqual(set(viewgen.PINNED_VARIANTS), set(viewgen.ROSTER_DBS))
        fleet = viewgen.pinned_fleet()
        self.assertEqual(set(fleet.dependencies), set(viewgen.ROSTER_DBS))
        self.assertEqual(set(fleet.issues), set(viewgen.ROSTER_DBS))

    def test_pinned_snapshot_is_actually_mixed(self):
        variants = set(viewgen.PINNED_VARIANTS.values())
        self.assertEqual(variants, {VARIANT_DEPS_LEGACY, VARIANT_DEPS_SPLIT})

    def test_deer_flow_pinned_split_regression(self):
        """The concrete regression: deer_flow migrated to the split schema
        while create_view.sql still selected depends_on_id, and dolt rejected
        the whole all_dependencies view."""
        self.assertEqual(viewgen.PINNED_VARIANTS["deer_flow"], VARIANT_DEPS_SPLIT)
        sql = viewgen.render_all_dependencies(viewgen.pinned_fleet())
        deer_arm = [a for a in sql.split("\nUNION ALL\n") if "`deer_flow`" in a]
        self.assertEqual(len(deer_arm), 1)
        self.assertIn("`depends_on_issue_id`", deer_arm[0])
        # The stale form. deer_flow is not the first arm, so there is no
        # `AS \`depends_on_id\`` alias to confuse this: any occurrence would be
        # a read of the column bd 1.2 dropped.
        self.assertNotIn("`depends_on_id`", deer_arm[0])


# --------------------------------------------------------------------------
# Mixed fleet + second migration, exercised as SQL against sqlite
# --------------------------------------------------------------------------

def build_sqlite_fleet(dep_variants: Dict[str, str],
                       rows: Dict[str, List[tuple]],
                       roster: Sequence[ProjectDB] = viewgen.ROSTER) -> sqlite3.Connection:
    """In-memory sqlite standing in for the dolt server: one ATTACHed schema
    per roster DB, each with a `dependencies` table in its variant's shape.

    `rows[db]` is a list of (issue_id, target, kind_hint) where kind_hint is
    'issue' | 'wisp' | 'external' | 'none'. For a legacy DB only 'issue' and
    'none' are representable, which is the point: legacy edges are
    issue-targeted by construction.
    """
    conn = sqlite3.connect(":memory:")
    for p in roster:
        conn.execute(f"ATTACH ':memory:' AS {viewgen.quote_ident(p.db)}")
        t = f"{viewgen.quote_ident(p.db)}.`dependencies`"
        if dep_variants[p.db] == VARIANT_DEPS_LEGACY:
            conn.execute(
                f"CREATE TABLE {t} (issue_id TEXT, depends_on_id TEXT, type TEXT, created_at TEXT)"
            )
        else:
            conn.execute(
                f"CREATE TABLE {t} (issue_id TEXT, depends_on_issue_id TEXT, "
                "depends_on_wisp_id TEXT, depends_on_external TEXT, type TEXT, created_at TEXT)"
            )
        for issue_id, target, kind in rows.get(p.db, []):
            if dep_variants[p.db] == VARIANT_DEPS_LEGACY:
                conn.execute(
                    f"INSERT INTO {t} VALUES (?,?,?,?)",
                    (issue_id, target if kind == "issue" else None, "blocks", "2026-09-06"),
                )
            else:
                conn.execute(
                    f"INSERT INTO {t} VALUES (?,?,?,?,?,?)",
                    (
                        issue_id,
                        target if kind == "issue" else None,
                        target if kind == "wisp" else None,
                        target if kind == "external" else None,
                        "blocks",
                        "2026-09-06",
                    ),
                )
    conn.commit()
    return conn


def sample_rows() -> Dict[str, List[tuple]]:
    """Every roster DB gets edges; split DBs also get wisp/external/all-NULL
    edges, which are exactly the rows a naive `depends_on_issue_id`-only arm
    would silently drop."""
    rows: Dict[str, List[tuple]] = {}
    for i, p in enumerate(viewgen.ROSTER):
        rows[p.db] = [(f"{p.project}-a{i}", f"{p.project}-b{i}", "issue")]
    rows["hearth"].append(("hearth-w", "wisp-123", "wisp"))
    rows["tengine"].append(("tengine-e", "https://example.invalid/x", "external"))
    rows["nervous_bus"].append(("nervous-bus-n", None, "none"))
    return rows


class TestMixedFleetUnion(unittest.TestCase):
    """The generated union executed for real, across both variants at once."""

    def setUp(self):
        self.variants = dict(PINNED_DEP_VARIANTS)
        self.rows = sample_rows()
        self.fleet = viewgen.FleetVariants(
            issues={p.db: VARIANT_ISSUES_CORE for p in viewgen.ROSTER},
            dependencies=self.variants,
        )
        self.conn = build_sqlite_fleet(self.variants, self.rows)
        self.body = viewgen.view_select_body(viewgen.render_all_dependencies(self.fleet))

    def tearDown(self):
        self.conn.close()

    def test_union_executes_across_both_variants(self):
        got = self.conn.execute(self.body).fetchall()
        self.assertTrue(got)

    def test_row_count_preserved_exactly(self):
        expected = sum(len(v) for v in self.rows.values())
        got = self.conn.execute(f"SELECT COUNT(*) FROM ({self.body})").fetchone()[0]
        self.assertEqual(got, expected)

    def test_every_project_contributes_its_rows(self):
        got = self.conn.execute(self.body).fetchall()
        by_issue = {r[0] for r in got}
        for db, rs in self.rows.items():
            for issue_id, _target, _kind in rs:
                self.assertIn(issue_id, by_issue, f"{db} lost edge {issue_id}")

    def test_edge_identity_preserved_by_kind(self):
        got = {r[0]: (r[1], r[2]) for r in self.conn.execute(self.body).fetchall()}
        self.assertEqual(got["hearth-w"], ("wisp-123", "wisp"))
        self.assertEqual(got["tengine-e"], ("https://example.invalid/x", "external"))
        self.assertEqual(got["nervous-bus-n"], (None, "unknown"))
        self.assertEqual(got["tengine-a8"][1], "issue")

    def test_legacy_arm_reports_issue_kind(self):
        got = {r[0]: r[2] for r in self.conn.execute(self.body).fetchall()}
        self.assertEqual(got["app-to-market-a0"], "issue")

    def test_wisp_and_external_edges_are_not_dropped(self):
        """A `depends_on_issue_id`-only arm would NULL these out; the COALESCE
        keeps them, and the kind column keeps them distinguishable so they
        still never join to all_issues."""
        got = self.conn.execute(
            f"SELECT COUNT(*) FROM ({self.body}) WHERE depends_on_kind IN ('wisp','external')"
        ).fetchone()[0]
        self.assertEqual(got, 2)

    def test_all_arms_project_identical_column_names(self):
        cur = self.conn.execute(f"SELECT * FROM ({self.body}) LIMIT 0")
        self.assertEqual(
            [d[0] for d in cur.description], list(viewgen.DEPENDENCY_VIEW_COLUMNS)
        )


class TestSecondMigration(unittest.TestCase):
    """A further DB flips legacy -> split after the snapshot was pinned.

    This is the recurrence case: it already happened once (deer_flow, after
    the 2026-08-31 snapshot). Regeneration must absorb it with a localised
    diff and no loss of edges or projects.
    """

    MIGRATING = "app_to_market"

    def setUp(self):
        self.before = dict(PINNED_DEP_VARIANTS)
        self.after = dict(PINNED_DEP_VARIANTS)
        self.assertEqual(self.before[self.MIGRATING], VARIANT_DEPS_LEGACY)
        self.after[self.MIGRATING] = VARIANT_DEPS_SPLIT

    def _fleet(self, variants):
        return viewgen.FleetVariants(
            issues={p.db: VARIANT_ISSUES_CORE for p in viewgen.ROSTER},
            dependencies=variants,
        )

    def test_migration_is_detected_from_column_shape(self):
        cmap = fleet_column_map(self.after)
        resolved = viewgen.resolve_fleet(cmap)
        self.assertEqual(resolved.dependencies[self.MIGRATING], VARIANT_DEPS_SPLIT)
        self.assertEqual(resolved.dependencies["temple_stuart_accounting"], VARIANT_DEPS_LEGACY)

    def test_only_the_migrated_arm_changes(self):
        before = viewgen.render_all_dependencies(self._fleet(self.before))
        after = viewgen.render_all_dependencies(self._fleet(self.after))
        self.assertNotEqual(before, after)
        b_arms = before.split("\nUNION ALL\n")
        a_arms = after.split("\nUNION ALL\n")
        self.assertEqual(len(b_arms), len(a_arms), "arm count must not change")
        changed = [i for i, (x, y) in enumerate(zip(b_arms, a_arms)) if x != y]
        # Arm 0 is app_to_market; it also carries the first-arm aliases, so
        # the alias text moves with it, but no OTHER arm may move.
        self.assertEqual(changed, [0])

    def test_all_projects_survive_the_migration(self):
        after = viewgen.render_all_dependencies(self._fleet(self.after))
        for p in viewgen.ROSTER:
            self.assertIn(f"{viewgen.quote_ident(p.db)}.`dependencies`", after)

    def test_stale_snapshot_against_migrated_db_is_caught(self):
        """The failure mode itself: the pinned snapshot still says legacy while
        the live DB has migrated. Preflight must report drift naming the DB."""
        conn = FakeDolt(self.after, deployed_views={"all_issues": 0, "all_dependencies": 0})
        report = viewgen.preflight(conn)
        self.assertTrue(
            any(self.MIGRATING in d for d in report.drift),
            f"drift not reported: {report.drift}",
        )

    def test_regenerated_union_still_preserves_counts(self):
        rows = sample_rows()
        rows[self.MIGRATING].append(("app-to-market-w", "wisp-9", "wisp"))
        conn = build_sqlite_fleet(self.after, rows)
        try:
            body = viewgen.view_select_body(
                viewgen.render_all_dependencies(self._fleet(self.after))
            )
            total = conn.execute(f"SELECT COUNT(*) FROM ({body})").fetchone()[0]
            self.assertEqual(total, sum(len(v) for v in rows.values()))
            kind = conn.execute(
                f"SELECT depends_on_kind FROM ({body}) WHERE issue_id = 'app-to-market-w'"
            ).fetchone()[0]
            self.assertEqual(kind, "wisp")
        finally:
            conn.close()


# --------------------------------------------------------------------------
# Preflight, against a fake DB-API connection
# --------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, parent: "FakeDolt"):
        self.parent = parent
        self._rows: List[tuple] = []
        self.description = None

    def execute(self, sql, params=None):
        self.parent.executed.append(sql)
        self._rows, self.description = self.parent.answer(sql, params)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeDolt:
    """Minimal stand-in for a pymysql connection over the dolt server.

    Answers the three query shapes viewgen.preflight issues:
    information_schema introspection, per-arm `SELECT * FROM (...) LIMIT 0`,
    and `SELECT COUNT(*) FROM ...` -- for both source tables and the deployed
    views. `broken_arms` / `deployed_views` model the real failure modes.
    """

    def __init__(self, dep_variants: Dict[str, str], *,
                 table_counts: Optional[Dict[Tuple[str, str], int]] = None,
                 deployed_views: Optional[Dict[str, object]] = None,
                 broken_arms: Sequence[Tuple[str, str]] = (),
                 missing_tables: Sequence[Tuple[str, str]] = ()):
        self.dep_variants = dep_variants
        self.table_counts = table_counts or {}
        self.deployed_views = deployed_views or {}
        self.broken_arms = set(broken_arms)
        self.missing_tables = set(missing_tables)
        self.executed: List[str] = []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass

    def _count(self, db, table):
        return self.table_counts.get((db, table), 1)

    def answer(self, sql, params):
        low = sql.lower()
        if "information_schema.columns" in low:
            rows = []
            for p in viewgen.ROSTER:
                if (p.db, "issues") not in self.missing_tables:
                    rows += [(p.db, "issues", c) for c in ISSUES_COLUMNS_FULL]
                if (p.db, "dependencies") not in self.missing_tables:
                    cols = (LEGACY_DEP_COLUMNS if self.dep_variants[p.db] == VARIANT_DEPS_LEGACY
                            else SPLIT_DEP_COLUMNS)
                    rows += [(p.db, "dependencies", c) for c in cols]
            return rows, None

        for db, table in self.broken_arms:
            if viewgen.qualified(db, table) in sql:
                raise RuntimeError(f"simulated dolt failure on {db}.{table}")

        if low.startswith("select count(*) from `beads_global`."):
            view = sql.split("`beads_global`.`")[1].split("`")[0]
            val = self.deployed_views.get(view)
            if isinstance(val, Exception):
                raise val
            if val is None:
                raise RuntimeError(
                    f"View 'beads_global.{view}' references invalid table(s) or column(s)"
                )
            return [(val,)], None

        if low.startswith("select count(*) from `"):
            db = sql.split("`")[1]
            table = sql.split("`")[3]
            return [(self._count(db, table),)], None

        # per-arm dry run / union dry run
        return [], None


def _counts_for(rows_per_table: int = 3) -> Dict[Tuple[str, str], int]:
    return {
        (p.db, t): rows_per_table
        for p in viewgen.ROSTER for t in ("issues", "dependencies")
    }


class TestPreflight(unittest.TestCase):
    def test_healthy_fleet_passes_with_parity(self):
        counts = _counts_for(3)
        total = 3 * len(viewgen.ROSTER)
        conn = FakeDolt(
            PINNED_DEP_VARIANTS, table_counts=counts,
            deployed_views={"all_issues": total, "all_dependencies": total},
        )
        report = viewgen.preflight(conn)
        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(len(report.arms), 2 * len(viewgen.ROSTER))
        self.assertTrue(all(a.ok for a in report.arms))
        self.assertEqual(report.drift, [])
        self.assertEqual(report.parity["all_issues"]["status"], "ok")
        self.assertEqual(report.parity["all_dependencies"]["status"], "ok")
        self.assertTrue(report.union_dryrun["all_dependencies"]["ok"])

    def test_broken_view_is_a_hard_fail_not_a_weak_success(self):
        """The live 2026-09-06 state: every source is fine, the deployed view
        is not. Preflight must FAIL rather than shrug and report source counts."""
        counts = _counts_for(3)
        conn = FakeDolt(
            PINNED_DEP_VARIANTS, table_counts=counts,
            deployed_views={"all_issues": 3 * len(viewgen.ROSTER)},  # all_dependencies absent
        )
        report = viewgen.preflight(conn)
        self.assertFalse(report.ok)
        self.assertEqual(report.parity["all_dependencies"]["status"], "view_unavailable")
        self.assertEqual(report.parity["all_issues"]["status"], "ok")

    def test_dropped_arm_detected_by_count_parity(self):
        """A view regenerated with one arm dropped applies cleanly and errors
        on nothing. Only count parity catches it."""
        counts = _counts_for(3)
        total = 3 * len(viewgen.ROSTER)
        conn = FakeDolt(
            PINNED_DEP_VARIANTS, table_counts=counts,
            deployed_views={"all_issues": total - 3, "all_dependencies": total},
        )
        report = viewgen.preflight(conn)
        self.assertFalse(report.ok)
        self.assertEqual(report.parity["all_issues"]["status"], "mismatch")
        self.assertIn("delta -3", report.parity["all_issues"]["detail"])

    def test_every_arm_probed_even_after_one_fails(self):
        """One run must enumerate all the damage; dolt's own error names no
        arm at all, which is why per-arm probing exists."""
        conn = FakeDolt(
            PINNED_DEP_VARIANTS, table_counts=_counts_for(3),
            deployed_views={"all_issues": 30, "all_dependencies": 30},
            broken_arms=[("hearth", "dependencies")],
        )
        report = viewgen.preflight(conn)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.arms), 2 * len(viewgen.ROSTER))
        failed = [a for a in report.arms if not a.ok]
        self.assertEqual([(a.db, a.table) for a in failed], [("hearth", "dependencies")])
        self.assertTrue(any(a.db == "tengine" and a.table == "dependencies" and a.ok
                            for a in report.arms))

    def test_missing_table_names_the_db_and_skips_dryrun(self):
        conn = FakeDolt(
            PINNED_DEP_VARIANTS, table_counts=_counts_for(3),
            deployed_views={"all_issues": 30, "all_dependencies": 30},
            missing_tables=[("sweepers_adventures", "dependencies")],
        )
        report = viewgen.preflight(conn)
        self.assertFalse(report.ok)
        bad = [a for a in report.arms if not a.ok]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].db, "sweepers_adventures")
        self.assertIn("table not found", bad[0].error)
        self.assertIsNone(report.union_dryrun["all_dependencies"]["ok"])
        self.assertEqual(report.parity["all_dependencies"]["status"], "unknown")


# --------------------------------------------------------------------------
# board.py: fail-closed view preflight + edge-identity in the blocked_by join
# --------------------------------------------------------------------------

def fake_view_definition(table: str, *, omit: Optional[str] = None,
                         quoted: bool = True) -> str:
    """A SHOW CREATE VIEW body shaped like what dolt actually returns: the
    view's original text, arm by arm. `quoted=False` reproduces the bare
    unquoted form the deployed all_issues was written in on 2026-09-06."""
    arms = []
    for p in viewgen.ROSTER:
        if p.db == omit:
            continue
        ref = (f"{viewgen.quote_ident(p.db)}.{viewgen.quote_ident(table)}"
               if quoted else f"{p.db}.{table}")
        arms.append(f"SELECT * FROM {ref}")
    return f"CREATE VIEW `x` AS " + "\nUNION ALL\n".join(arms)


class TestDbReferencedIn(unittest.TestCase):
    """Arm-omission detection reads the view's own definition text, so it has
    to survive however that text was originally quoted."""

    def test_matches_backtick_quoted_reference(self):
        self.assertTrue(viewgen.db_referenced_in("FROM `deer_flow`.`issues`", "deer_flow"))

    def test_matches_bare_unquoted_reference(self):
        """The live 2026-09-06 all_issues was written unquoted. A
        backtick-only substring test called all ten arms missing."""
        self.assertTrue(viewgen.db_referenced_in("FROM deer_flow.issues", "deer_flow"))

    def test_absent_db_not_matched(self):
        self.assertFalse(viewgen.db_referenced_in("FROM deer_flow.issues", "tengine"))

    def test_hearth_does_not_match_inside_hearth_loom(self):
        """`hearth` is a prefix of `hearth-loom`; a loose substring test would
        report hearth present in a view that only reads hearth-loom."""
        only_loom = "FROM `hearth-loom`.`issues`"
        self.assertFalse(viewgen.db_referenced_in(only_loom, "hearth"))
        self.assertTrue(viewgen.db_referenced_in(only_loom, "hearth-loom"))
        self.assertFalse(viewgen.db_referenced_in("FROM hearth-loom.issues", "hearth"))

    def test_requires_the_table_qualifier_dot(self):
        # A bare mention in a comment is not a read.
        self.assertFalse(viewgen.db_referenced_in("-- see tengine for details", "tengine"))

    def test_every_roster_db_found_in_both_quoting_styles(self):
        for quoted in (True, False):
            definition = fake_view_definition("issues", quoted=quoted)
            for p in viewgen.ROSTER:
                self.assertTrue(
                    viewgen.db_referenced_in(definition, p.db),
                    f"{p.db} not found (quoted={quoted})",
                )


class FakeViewCursor:
    def __init__(self, parent: "FakeViewConn"):
        self.parent = parent
        self._rows: List[tuple] = []
        self.description = None

    def execute(self, sql, params=None):
        self.parent.executed.append(sql)
        low = sql.lower()
        if low.startswith("show create view"):
            view = sql.split("`beads_global`.`")[1].split("`")[0]
            self._rows = [(view, self.parent.definitions[view])]
            self.description = None
            return
        if low.startswith("select * from `beads_global`."):
            view = sql.split("`beads_global`.`")[1].split("`")[0]
            cols = self.parent.columns.get(view)
            if cols is None:
                raise RuntimeError(f"View 'beads_global.{view}' references invalid table(s)")
            self.description = [(c,) for c in cols]
            self._rows = []
            return
        self._rows = list(self.parent.join_rows)
        self.description = None

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeViewConn:
    def __init__(self, *, columns=None, definitions=None, join_rows=()):
        self.columns = columns if columns is not None else {
            "all_issues": list(viewgen.ISSUES_VIEW_COLUMNS),
            "all_dependencies": list(viewgen.DEPENDENCY_VIEW_COLUMNS),
        }
        self.definitions = definitions if definitions is not None else {
            "all_issues": fake_view_definition("issues"),
            "all_dependencies": fake_view_definition("dependencies"),
        }
        self.join_rows = join_rows
        self.executed: List[str] = []

    def cursor(self):
        return FakeViewCursor(self)


class TestBoardPreflightViews(unittest.TestCase):
    def test_healthy_views_pass(self):
        conn = FakeViewConn()
        evidence = board.preflight_board_views(conn)
        self.assertTrue(evidence["all_issues"]["queryable"])
        self.assertEqual(evidence["all_dependencies"]["missing_columns"], [])
        self.assertEqual(evidence["all_dependencies"]["omitted_roster_dbs"], [])

    def test_unqueryable_view_fails_closed(self):
        conn = FakeViewConn(columns={"all_issues": list(viewgen.ISSUES_VIEW_COLUMNS)})
        with self.assertRaises(board.BoardSourceError) as ctx:
            board.preflight_board_views(conn)
        self.assertIn("all_dependencies", str(ctx.exception))
        self.assertIn("create_view.sql", str(ctx.exception))

    def test_stale_view_missing_kind_column_fails_closed(self):
        """A pre-migration all_dependencies would otherwise blow up mid-run in
        the blocked_by query with no hint about what to do."""
        conn = FakeViewConn(columns={
            "all_issues": list(viewgen.ISSUES_VIEW_COLUMNS),
            "all_dependencies": ["issue_id", "depends_on_id", "type", "created_at"],
        })
        with self.assertRaises(board.BoardSourceError) as ctx:
            board.preflight_board_views(conn)
        self.assertIn("depends_on_kind", str(ctx.exception))

    def test_dropped_arm_in_view_definition_fails_closed(self):
        """The silent one: a view regenerated without tengine's arm queries
        fine and returns plausible rows, while every tengine bead vanishes."""
        partial_i = fake_view_definition("issues", omit="tengine")
        partial_d = fake_view_definition("dependencies", omit="tengine")
        conn = FakeViewConn(definitions={"all_issues": partial_i, "all_dependencies": partial_d})
        with self.assertRaises(board.BoardSourceError) as ctx:
            board.preflight_board_views(conn)
        self.assertIn("tengine", str(ctx.exception))
        self.assertIn("omits roster DB", str(ctx.exception))

    def test_unquoted_view_definition_is_not_a_false_omission(self):
        """Regression: the deployed all_issues was written with bare DB names
        and count-parity clean, yet the first cut of this check flagged nine
        of ten arms as dropped."""
        conn = FakeViewConn(definitions={
            "all_issues": fake_view_definition("issues", quoted=False),
            "all_dependencies": fake_view_definition("dependencies", quoted=False),
        })
        evidence = board.preflight_board_views(conn)
        self.assertEqual(evidence["all_issues"]["omitted_roster_dbs"], [])

    def test_empty_project_is_not_mistaken_for_a_dropped_arm(self):
        """Checking the DEFINITION, not DISTINCT project values, is what keeps
        a legitimately-empty project (app_to_market has 2 issues and 0
        dependency rows live) from tripping the omission check."""
        conn = FakeViewConn()
        board.preflight_board_views(conn)  # no rows anywhere; must not raise


class TestMainFailsClosed(unittest.TestCase):
    def test_source_error_exits_nonzero_and_writes_nothing(self):
        """A run against broken views must not leave a board.json behind that
        looks fresh and under-reports blockers."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            out = pathlib.Path(td) / "board.json"
            rep = pathlib.Path(td) / "report.md"
            orig = board.run

            def boom(**kwargs):
                raise board.BoardSourceError("simulated: all_dependencies is not queryable")

            board.run = boom
            try:
                rc = board.main([
                    "--board-file", str(out), "--report-file", str(rep),
                ])
            finally:
                board.run = orig

        self.assertEqual(rc, board.EXIT_SOURCE_UNAVAILABLE)
        self.assertNotEqual(rc, 0)
        self.assertFalse(out.exists())
        self.assertFalse(rep.exists())


class TestBlockedByEdgeIdentity(unittest.TestCase):
    def test_join_restricted_to_issue_kind_edges(self):
        conn = FakeViewConn(join_rows=[("a", "b"), ("a", "c"), ("d", "e")])
        out = board.fetch_open_blocked_by(conn)
        sql = conn.executed[-1]
        self.assertIn("d.depends_on_kind = 'issue'", sql)
        self.assertEqual(out, {"a": ["b", "c"], "d": ["e"]})

    def test_status_semantics_unchanged(self):
        """The migration must not quietly redefine what 'blocked' means."""
        conn = FakeViewConn(join_rows=[])
        board.fetch_open_blocked_by(conn)
        sql = conn.executed[-1]
        self.assertIn("d.type = 'blocks'", sql)
        self.assertIn("blocker.status != 'closed'", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
