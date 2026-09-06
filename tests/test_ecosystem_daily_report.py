#!/usr/bin/env python3
"""Tests for tools/ecosystem_daily_report.py.

The cases here are the ones the live data actually produces: a rotated journal
whose generations overlap, an envelope id reused for two different events, a
timestamp with no offset, a producer that reports no phase or cost, and a
fixture-classification question the data cannot answer. Each test pins the
behaviour that keeps the report honest rather than merely non-crashing.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import ecosystem_daily_report as edr  # noqa: E402


START = datetime(2026, 9, 5, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 9, 6, 0, 0, 0, tzinfo=timezone.utc)


def envelope(
    *,
    eid: str,
    etype: str = "bus.agent.activity.v1",
    time: str = "2026-09-05T12:00:00Z",
    source: str = "/claude-host/demo",
    data: dict | None = None,
    **extras,
) -> dict:
    payload = {
        "specversion": "1.0",
        "id": eid,
        "source": source,
        "type": etype,
        "time": time,
        "datacontenttype": "application/json",
        "data": data if data is not None else {},
    }
    payload.update(extras)
    return payload


def activity(**data) -> dict:
    base = {
        "ts": "2026-09-05T12:00:00Z",
        "event": "tool_call",
        "agent_kind": "host_claude_code",
        "agent_id": "agent-1",
        "session_id": "sess-1",
        "project": "demo",
    }
    base.update(data)
    return base


def write_journal(directory: Path, generations: dict[str, list[dict]]) -> Path:
    """Write a live journal plus gzip rotations.

    ``generations`` maps a suffix ("" for the live file, "1"/"2"/... for the
    rotations) to the envelopes that generation holds.
    """
    live = directory / "debug.jsonl"
    for suffix, records in generations.items():
        if suffix == "":
            with open(live, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
        else:
            with gzip.open(f"{live}.{suffix}.gz", "wt", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
    if "" not in generations:
        live.write_text("", encoding="utf-8")
    return live


def run(journal: Path, db: Path | None = None, **kwargs):
    return edr.build_report(
        start=kwargs.pop("start", START),
        end=kwargs.pop("end", END),
        journal=journal,
        db=db if db is not None else journal.parent / "does-not-exist.db",
        **kwargs,
    )


# ── window resolution ────────────────────────────────────────────────────────


class TestWindowResolution(unittest.TestCase):
    def test_hours_anchors_on_until(self):
        start, end = edr.resolve_window(hours=6, since=None, until="2026-09-06T00:00:00Z")
        self.assertEqual(edr._iso(start), "2026-09-05T18:00:00Z")
        self.assertEqual(edr._iso(end), "2026-09-06T00:00:00Z")

    def test_since_and_hours_are_mutually_exclusive(self):
        with self.assertRaises(edr.ReportError):
            edr.resolve_window(hours=1, since="2026-09-05T00:00:00Z", until=None)

    def test_default_is_24h_back_from_now(self):
        now = datetime(2026, 9, 6, 3, 0, 0, tzinfo=timezone.utc)
        start, end = edr.resolve_window(hours=None, since=None, until=None, now=now)
        self.assertEqual(end - start, timedelta(hours=24))
        self.assertEqual(end, now)

    def test_offset_timestamps_normalise_to_utc(self):
        start, _ = edr.resolve_window(
            hours=None, since="2026-09-05T02:00:00+02:00", until="2026-09-06T00:00:00Z"
        )
        self.assertEqual(edr._iso(start), "2026-09-05T00:00:00Z")

    def test_inverted_window_is_refused(self):
        with self.assertRaises(edr.ReportError):
            edr.resolve_window(
                hours=None, since="2026-09-06T00:00:00Z", until="2026-09-05T00:00:00Z"
            )

    def test_naive_cli_timestamp_is_refused(self):
        with self.assertRaises(ValueError):
            edr.resolve_window(hours=None, since="2026-09-05T00:00:00", until=None)


# ── rotation + duplicates ────────────────────────────────────────────────────


class TestRotationAndDuplicates(unittest.TestCase):
    def test_rotations_are_read_oldest_first(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = write_journal(
                directory,
                {
                    "2": [envelope(eid="old", time="2026-09-05T01:00:00Z")],
                    "1": [envelope(eid="mid", time="2026-09-05T02:00:00Z")],
                    "": [envelope(eid="new", time="2026-09-05T03:00:00Z")],
                },
            )
            names = [Path(p).name for p in edr.discover_journal_files(live)]
            self.assertEqual(
                names, ["debug.jsonl.2.gz", "debug.jsonl.1.gz", "debug.jsonl"]
            )

    def test_duplicate_across_rotation_boundary_collapses_once(self):
        """A rotation copy of the same envelope must not double-count."""
        shared = envelope(eid="dup-1", time="2026-09-05T10:00:00Z")
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = write_journal(directory, {"1": [shared], "": [dict(shared)]})
            report = run(live)
            records = report["journal"]["records"]
            self.assertEqual(records["raw_in_window"], 2)
            self.assertEqual(records["unique"], 1)
            self.assertEqual(records["exact_duplicates_collapsed"], 1)
            self.assertEqual(records["id_collisions_kept"], 0)

    def test_duplicate_with_reordered_keys_still_collapses(self):
        original = envelope(eid="dup-2", time="2026-09-05T10:00:00Z")
        reordered = {key: original[key] for key in reversed(list(original))}
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"1": [original], "": [reordered]})
            report = run(live)
            self.assertEqual(report["journal"]["records"]["unique"], 1)
            self.assertEqual(report["journal"]["records"]["exact_duplicates_collapsed"], 1)

    def test_same_id_different_payload_is_kept_and_flagged(self):
        """The live journal really does this; collapsing on id would lose events."""
        first = envelope(
            eid="codex-collide",
            etype="agent.session.v1",
            time="2026-09-05T04:20:41Z",
            data={"event": "compact", "ts": "2026-09-05T04:20:41Z"},
        )
        second = envelope(
            eid="codex-collide",
            etype="agent.session.v1",
            time="2026-09-05T04:26:37Z",
            data={"event": "compact", "ts": "2026-09-05T04:26:37Z"},
        )
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": [first, second]})
            report = run(live)
            records = report["journal"]["records"]
            self.assertEqual(records["raw_in_window"], 2)
            self.assertEqual(records["unique"], 2, "a collision must never be dropped")
            self.assertEqual(records["exact_duplicates_collapsed"], 0)
            self.assertEqual(records["id_collisions_kept"], 1)
            sample = report["journal"]["id_collision_samples"][0]
            self.assertEqual(sample["id"], "codex-collide")
            self.assertEqual(sample["first_time"], "2026-09-05T04:20:41Z")
            self.assertEqual(sample["second_time"], "2026-09-05T04:26:37Z")

    def test_collision_samples_are_capped_but_counts_are_not(self):
        records = [
            envelope(eid="same", time=f"2026-09-05T10:00:{n:02d}Z") for n in range(12)
        ]
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": records})
            report = run(live, max_samples=3)
            self.assertEqual(report["journal"]["records"]["id_collisions_kept"], 11)
            self.assertEqual(len(report["journal"]["id_collision_samples"]), 3)

    def test_records_without_id_are_counted_not_deduped(self):
        anonymous = {
            "specversion": "1.0",
            "source": "/x",
            "type": "bus.notify.v1",
            "time": "2026-09-05T10:00:00Z",
            "data": {},
        }
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": [anonymous, dict(anonymous)]})
            report = run(live)
            self.assertEqual(report["journal"]["records"]["records_without_id"], 2)
            self.assertEqual(report["journal"]["records"]["unique"], 2)

    def test_id_tracking_cap_is_reported(self):
        records = [envelope(eid=f"id-{n}", time="2026-09-05T10:00:00Z") for n in range(10)]
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": records})
            report = run(live, max_tracked_ids=4)
            self.assertTrue(report["journal"]["records"]["id_tracking_truncated"])
            self.assertEqual(report["journal"]["records"]["distinct_ids_tracked"], 4)


# ── malformed time ───────────────────────────────────────────────────────────


class TestMalformedTime(unittest.TestCase):
    def _report_for(self, records):
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": records})
            return run(live)

    def test_naive_timestamp_is_excluded_and_counted(self):
        report = self._report_for(
            [
                envelope(eid="naive", time="2026-09-05T12:00:00"),
                envelope(eid="good", time="2026-09-05T12:00:00Z"),
            ]
        )
        parse = report["journal"]["parse"]
        self.assertEqual(parse["time_malformed"], 1)
        self.assertEqual(report["journal"]["records"]["unique"], 1)
        self.assertIn("no UTC offset", parse["time_malformed_samples"][0]["reason"])

    def test_garbage_timestamp_is_excluded(self):
        report = self._report_for([envelope(eid="junk", time="not-a-time")])
        self.assertEqual(report["journal"]["parse"]["time_malformed"], 1)
        self.assertEqual(report["journal"]["records"]["unique"], 0)

    def test_missing_time_field_is_its_own_bucket(self):
        record = envelope(eid="x")
        record.pop("time")
        report = self._report_for([record])
        self.assertEqual(report["journal"]["parse"]["time_field_missing"], 1)
        self.assertEqual(report["journal"]["parse"]["time_malformed"], 0)

    def test_unparseable_line_is_counted_not_fatal(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = directory / "debug.jsonl"
            live.write_text(
                json.dumps(envelope(eid="ok")) + "\n{not json\n\n", encoding="utf-8"
            )
            report = run(live)
            self.assertEqual(report["journal"]["parse"]["json_errors"], 1)
            self.assertEqual(report["journal"]["records"]["unique"], 1)

    def test_window_is_half_open(self):
        report = self._report_for(
            [
                envelope(eid="at-start", time="2026-09-05T00:00:00Z"),
                envelope(eid="at-end", time="2026-09-06T00:00:00Z"),
            ]
        )
        self.assertEqual(report["journal"]["records"]["unique"], 1)
        self.assertEqual(report["journal"]["parse"]["records_outside_window"], 1)

    def test_offset_timestamp_inside_window_is_included(self):
        report = self._report_for(
            [envelope(eid="offset", time="2026-09-05T14:00:00+02:00")]
        )
        self.assertEqual(report["journal"]["records"]["unique"], 1)
        self.assertEqual(report["journal"]["records"]["earliest_utc"], "2026-09-05T12:00:00Z")


# ── missing phase / cost ─────────────────────────────────────────────────────


class TestMissingPhaseAndCost(unittest.TestCase):
    def test_absent_usage_is_unknown_never_zero(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(
                Path(tmp), {"": [envelope(eid="a", data=activity())]}
            )
            report = run(live)
            metrics = report["journal"]["usage"]["metrics"]
            self.assertEqual(metrics["input_tokens"]["unknown_count"], 1)
            self.assertEqual(metrics["input_tokens"]["known_count"], 0)
            self.assertEqual(metrics["input_tokens"]["zero_count"], 0)

    def test_explicit_zero_is_distinct_from_unknown(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(
                Path(tmp),
                {
                    "": [
                        envelope(eid="a", data=activity(input_tokens=0)),
                        envelope(eid="b", data=activity(input_tokens=None)),
                        envelope(eid="c", data=activity(input_tokens=7)),
                    ]
                },
            )
            report = run(live)
            tokens = report["journal"]["usage"]["metrics"]["input_tokens"]
            self.assertEqual(tokens["known_sum"], 7)
            self.assertEqual(tokens["known_count"], 2)
            self.assertEqual(tokens["zero_count"], 1)
            self.assertEqual(tokens["unknown_count"], 1)

    def test_non_numeric_usage_is_invalid_not_summed(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(
                Path(tmp), {"": [envelope(eid="a", data=activity(input_tokens="many"))]}
            )
            report = run(live)
            tokens = report["journal"]["usage"]["metrics"]["input_tokens"]
            self.assertEqual(tokens["invalid_count"], 1)
            self.assertEqual(tokens["known_count"], 0)
            self.assertEqual(tokens["unknown_count"], 0)

    def test_fractional_cost_is_summed_exactly_as_a_string(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(
                Path(tmp),
                {
                    "": [
                        envelope(eid="a", data=activity(cost_usd=0.1)),
                        envelope(eid="b", data=activity(cost_usd=0.2)),
                    ]
                },
            )
            report = run(live)
            cost = report["journal"]["usage"]["metrics"]["cost_usd"]
            self.assertEqual(cost["known_sum"], "0.3")
            self.assertEqual(cost["known_count"], 2)

    def test_missing_identity_is_reported_against_a_named_denominator(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(
                Path(tmp),
                {
                    "": [
                        envelope(eid="a", data=activity()),
                        envelope(eid="b", data=activity(worktree="wt-1")),
                    ]
                },
            )
            report = run(live)
            worktree = report["journal"]["identity_coverage"]["worktree"]
            self.assertEqual(worktree["present"], 1)
            self.assertEqual(worktree["missing"], 1)
            self.assertEqual(worktree["denominator"], 2)
            self.assertIn("bus.agent.activity.v1", worktree["denominator_basis"])
            self.assertEqual(report["journal"]["identity_coverage"]["bead_id"]["present"], 0)

    def test_worker_lifecycle_outcome_missing_is_its_own_bucket(self):
        lifecycle = envelope(
            eid="w1",
            etype="orca.worker.lifecycle.v1",
            data={
                "run_id": "run_1",
                "task_id": "task_1",
                "dispatch_id": "ctx_1",
                "bead_id": None,
                "state": "dispatched",
                "outcome": None,
                "worktree": "/w/proj/a",
            },
        )
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": [lifecycle]})
            report = run(live)
            lifecycle_report = report["journal"]["orca_worker_lifecycle"]
            self.assertEqual(lifecycle_report["distinct_task_ids"], 1)
            self.assertEqual(lifecycle_report["distinct_bead_ids"], 0)
            self.assertEqual(lifecycle_report["outcomes"]["counts"]["<unknown>"], 1)


# ── trace handling ───────────────────────────────────────────────────────────


class TestTrace(unittest.TestCase):
    def test_absent_traceparent_reports_no_producer_and_invents_nothing(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": [envelope(eid="a", data=activity())]})
            report = run(live)
            trace = report["journal"]["trace"]
            self.assertEqual(trace["envelopes_with_traceparent"], 0)
            self.assertEqual(trace["distinct_trace_ids"], 0)
            self.assertEqual(trace["producer_status"], "no_producer_observed")

    def test_traceparent_extension_is_preserved_and_grouped(self):
        tp = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        with TemporaryDirectory() as tmp:
            live = write_journal(
                Path(tmp),
                {
                    "": [
                        envelope(eid="a", data=activity(), traceparent=tp),
                        envelope(
                            eid="b",
                            etype="bus.bead.lifecycle.v1",
                            data={},
                            traceparent=tp,
                        ),
                    ]
                },
            )
            report = run(live)
            trace = report["journal"]["trace"]
            self.assertEqual(trace["envelopes_with_traceparent"], 2)
            self.assertEqual(trace["distinct_trace_ids"], 1)
            self.assertEqual(trace["trace_ids_spanning_multiple_types"], 1)
            self.assertEqual(trace["producer_status"], "observed")
            self.assertEqual(report["journal"]["identity_coverage"]["traceparent"]["present"], 1)

    def test_malformed_traceparent_is_counted_not_grouped(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(
                Path(tmp), {"": [envelope(eid="a", data=activity(), traceparent="nope")]}
            )
            report = run(live)
            trace = report["journal"]["trace"]
            self.assertEqual(trace["malformed_traceparent"], 1)
            self.assertEqual(trace["distinct_trace_ids"], 0)


# ── fixture classification limits ────────────────────────────────────────────


class TestFixtureClassification(unittest.TestCase):
    def test_unmarked_traffic_is_unclassified_not_production(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": [envelope(eid="a", data=activity())]})
            report = run(live)
            counts = report["journal"]["classification"]["counts"]
            self.assertEqual(counts.get("production_or_unclassified"), 1)
            self.assertNotIn("production", counts)
            self.assertIn("NOT proof of production", report["journal"]["classification"]["note"])

    def test_declared_fixture_is_separated(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(
                Path(tmp),
                {
                    "": [
                        envelope(eid="a", etype="test.channel", data={}),
                        envelope(eid="b", data=activity(fixture=True)),
                        envelope(eid="c", data=activity()),
                    ]
                },
            )
            report = run(live)
            counts = report["journal"]["classification"]["counts"]
            self.assertEqual(counts["explicit_fixture"], 2)
            self.assertEqual(counts["production_or_unclassified"], 1)

    def test_limitations_name_the_classification_bound(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": [envelope(eid="a", data=activity())]})
            report = run(live)
            joined = " ".join(report["limitations"])
            self.assertIn("self-declared", joined)
            self.assertIn("not proof of production traffic", joined)

    def test_report_never_claims_verified_acceptance(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": [envelope(eid="a", data=activity())]})
            report = run(live)
            self.assertEqual(report["verified_acceptance"], "NOT_EVALUATED")
            self.assertEqual(report["evidence_level"], "observed_telemetry")


# ── project vs worktree, freshness, bounds ───────────────────────────────────


class TestProjectWorktreeAndFreshness(unittest.TestCase):
    def test_project_and_worktree_are_counted_separately(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(
                Path(tmp),
                {
                    "": [
                        envelope(eid="a", data=activity(project="p1", worktree="wt-a")),
                        envelope(eid="b", data=activity(project="p1", worktree="wt-b")),
                    ]
                },
            )
            report = run(live)
            self.assertEqual(report["journal"]["by_project"]["counts"]["p1"], 2)
            self.assertEqual(report["journal"]["by_worktree_slug"]["distinct"], 2)

    def test_slug_shared_across_projects_is_flagged_as_ambiguous(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(
                Path(tmp),
                {
                    "": [
                        envelope(eid="a", data=activity(project="p1", worktree="shared")),
                        envelope(eid="b", data=activity(project="p2", worktree="shared")),
                    ]
                },
            )
            report = run(live)
            ambiguous = report["journal"]["project_worktree_distinction"][
                "worktree_slugs_seen_under_multiple_projects"
            ]
            self.assertEqual(ambiguous["shared"], ["p1", "p2"])

    def test_source_seen_only_outside_window_reports_staleness(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(
                Path(tmp),
                {
                    "1": [
                        envelope(eid="old", source="/stale", time="2026-09-04T00:00:00Z")
                    ],
                    "": [envelope(eid="new", source="/live", time="2026-09-05T12:00:00Z")],
                },
            )
            report = run(live)
            sources = {
                entry["source"]: entry
                for entry in report["journal"]["source_freshness"]["sources"]
            }
            self.assertFalse(sources["/stale"]["emitted_in_window"])
            self.assertEqual(sources["/stale"]["records_in_window"], 0)
            self.assertTrue(sources["/live"]["emitted_in_window"])
            self.assertEqual(
                sources["/live"]["staleness_seconds"],
                int((END - datetime(2026, 9, 5, 12, tzinfo=timezone.utc)).total_seconds()),
            )

    def test_cardinality_cap_folds_overflow_and_flags_it(self):
        records = [
            envelope(eid=f"e{n}", data=activity(project=f"proj-{n}")) for n in range(10)
        ]
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": records})
            report = run(live, max_cardinality=3)
            by_project = report["journal"]["by_project"]
            self.assertTrue(by_project["truncated"])
            self.assertIn("<other>", by_project["counts"])
            self.assertEqual(sum(by_project["counts"].values()), 10)

    def test_denominators_are_named_and_not_interchangeable(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": [envelope(eid="a", data=activity())]})
            report = run(live)
            denominators = report["denominators"]
            self.assertEqual(denominators["records"]["value"], 1)
            self.assertIsNone(denominators["segments"]["value"])
            self.assertEqual(denominators["tasks"]["value"], 0)
            self.assertIn("basis", denominators["records"])


# ── recorder database ────────────────────────────────────────────────────────


def make_db(path: Path, runs: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, run_key TEXT NOT NULL,
            run_key_kind TEXT NOT NULL, host_conversation_id TEXT,
            project TEXT NOT NULL, agent_kind TEXT NOT NULL,
            session_id TEXT, agent_id TEXT, started TEXT NOT NULL,
            ended TEXT NOT NULL, close_reason TEXT, continues_run_id TEXT,
            event_count INTEGER NOT NULL DEFAULT 0,
            tool_histogram TEXT NOT NULL DEFAULT '{}',
            worktree TEXT, worktree_slug TEXT, git_branch TEXT,
            bead_id TEXT, outcome TEXT, labeled_at TEXT, label_version INTEGER,
            label_history TEXT NOT NULL DEFAULT '[]',
            features TEXT NOT NULL DEFAULT '{}',
            schema_version TEXT NOT NULL DEFAULT '1',
            recorded_at TEXT NOT NULL
        )
        """
    )
    for row in runs:
        conn.execute(
            "INSERT INTO runs (run_id, run_key, run_key_kind, project, agent_kind, "
            "started, ended, worktree, worktree_slug, bead_id, outcome, event_count, "
            "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["run_id"],
                row.get("run_key", row["run_id"]),
                row.get("run_key_kind", "worktree"),
                row.get("project", "demo"),
                row.get("agent_kind", "host_subagent"),
                row["started"],
                row["ended"],
                row.get("worktree"),
                row.get("worktree_slug"),
                row.get("bead_id"),
                row.get("outcome"),
                row.get("event_count", 0),
                row["started"],
            ),
        )
    conn.commit()
    conn.close()


class TestRecorderDatabase(unittest.TestCase):
    def test_absent_database_is_unavailable_not_zero(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": []})
            report = run(live)
            self.assertEqual(report["recorder_db"]["status"], "absent")
            self.assertIsNone(report["denominators"]["segments"]["value"])

    def test_segments_use_overlap_and_report_identity_coverage(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = write_journal(directory, {"": []})
            db = directory / "runs.db"
            make_db(
                db,
                [
                    # starts before the window, still open inside it -> counted
                    {
                        "run_id": "r1",
                        "started": "2026-09-04T23:00:00Z",
                        "ended": "2026-09-05T01:00:00Z",
                        "worktree_slug": "wt-a",
                        "bead_id": "demo-abc12",
                    },
                    {
                        "run_id": "r2",
                        "started": "2026-09-05T10:00:00Z",
                        "ended": "2026-09-05T11:00:00Z",
                        "worktree_slug": "wt-b",
                    },
                    # entirely before the window -> excluded
                    {
                        "run_id": "r3",
                        "started": "2026-09-01T00:00:00Z",
                        "ended": "2026-09-01T01:00:00Z",
                    },
                ],
            )
            report = run(live, db=db)
            segments = report["recorder_db"]["segments"]
            self.assertEqual(segments["count"], 2)
            self.assertEqual(segments["identity_coverage"]["bead_id"]["present"], 1)
            self.assertEqual(segments["identity_coverage"]["bead_id"]["missing"], 1)
            self.assertEqual(
                segments["identity_coverage"]["worktree_absolute_path"]["present"], 0
            )
            self.assertEqual(report["denominators"]["segments"]["value"], 2)

    def test_database_is_opened_read_only(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            db = directory / "runs.db"
            make_db(db, [])
            conn = edr._connect_readonly(db)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("DELETE FROM runs")
            finally:
                conn.close()


# ── derived correlation ──────────────────────────────────────────────────────


class TestIdentityCorrelation(unittest.TestCase):
    def _journal_with_lifecycle(self, directory: Path, worktrees: list[tuple[str, str]]):
        records = [
            envelope(
                eid=f"w{index}",
                etype="orca.worker.lifecycle.v1",
                data={
                    "run_id": f"run_{index}",
                    "task_id": task_id,
                    "dispatch_id": f"ctx_{index}",
                    "bead_id": None,
                    "worktree": path,
                },
            )
            for index, (path, task_id) in enumerate(worktrees)
        ]
        return write_journal(directory, {"": records})

    def test_unique_match_by_absolute_path(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = self._journal_with_lifecycle(
                directory, [("/home/eric/data2/worktrees/demo/wt-a", "task_1")]
            )
            db = directory / "runs.db"
            make_db(
                db,
                [
                    {
                        "run_id": "r1",
                        "started": "2026-09-05T10:00:00Z",
                        "ended": "2026-09-05T11:00:00Z",
                        "worktree": "/home/eric/data2/worktrees/demo/wt-a",
                        "worktree_slug": "wt-a",
                    }
                ],
            )
            correlation = run(live, db=db)["identity_correlation"]
            self.assertEqual(correlation["matched_unique"], 1)
            self.assertEqual(correlation["matched_ambiguous"], 0)
            self.assertEqual(correlation["unmatched"], 0)
            self.assertEqual(correlation["authority"], "observational_join_only")

    def test_reused_worktree_is_ambiguous_not_resolved(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = self._journal_with_lifecycle(
                directory,
                [
                    ("/home/eric/data2/worktrees/demo/wt-a", "task_1"),
                    ("/home/eric/data2/worktrees/demo/wt-a", "task_2"),
                ],
            )
            db = directory / "runs.db"
            make_db(
                db,
                [
                    {
                        "run_id": "r1",
                        "started": "2026-09-05T10:00:00Z",
                        "ended": "2026-09-05T11:00:00Z",
                        "worktree": "/home/eric/data2/worktrees/demo/wt-a",
                        "worktree_slug": "wt-a",
                    }
                ],
            )
            correlation = run(live, db=db)["identity_correlation"]
            self.assertEqual(correlation["matched_ambiguous"], 1)
            self.assertEqual(correlation["matched_unique"], 0)

    def test_missing_absolute_path_remains_unmatched(self):
        """Historical NULL worktree rows lack enough evidence for an identity join."""
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = self._journal_with_lifecycle(
                directory, [("/home/eric/data2/worktrees/demo/wt-a", "task_1")]
            )
            db = directory / "runs.db"
            make_db(
                db,
                [
                    {
                        "run_id": "r1",
                        "started": "2026-09-05T10:00:00Z",
                        "ended": "2026-09-05T11:00:00Z",
                        "worktree": None,
                        "worktree_slug": "wt-a",
                    }
                ],
            )
            correlation = run(live, db=db)["identity_correlation"]
            self.assertEqual(correlation["matched_unique"], 0)
            self.assertEqual(correlation["unmatched"], 1)

    def test_correlation_unavailable_without_a_database(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": []})
            correlation = run(live)["identity_correlation"]
            self.assertEqual(correlation["status"], "unavailable")


# ── optional attempt ledger ──────────────────────────────────────────────────


def ledger_record(**overrides) -> dict:
    record = {
        "attempt_id": "att-1",
        "execution_id": "exe-1",
        "bead_id": "nervous-bus-5odb",
        "project": "nervous-bus",
        "model": "claude-opus-5",
        "task_id": "task-1",
        "condition": "packet",
        "corpus_sha256": "a" * 64,
        "status": "completed",
        "identities": {"orca_run_id": "run_1"},
        # Every phase must carry every metric; an unmeasured metric is an
        # explicit null, never an absent key.
        "costs": {
            phase: {
                "duration_ms": None,
                "input_tokens": None,
                "output_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "cost_usd": None,
            }
            for phase in (
                "construction",
                "research",
                "retrieval",
                "execution",
                "retry",
                "review",
            )
        },
        "receipt": None,
    }
    record.update(overrides)
    return record


class TestAttemptLedger(unittest.TestCase):
    def test_not_supplied_is_explicit(self):
        with TemporaryDirectory() as tmp:
            live = write_journal(Path(tmp), {"": []})
            self.assertEqual(run(live)["attempt_ledger"]["status"], "not_supplied")

    def test_valid_ledger_is_summarised_through_the_existing_contract(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = write_journal(directory, {"": []})
            ledger = directory / "ledger.jsonl"
            ledger.write_text(json.dumps(ledger_record()) + "\n", encoding="utf-8")
            summary = run(live, ledger=ledger)["attempt_ledger"]
            self.assertEqual(summary["status"], "summarized")
            self.assertEqual(summary["report"]["attempts"], 1)
            # An attempt without a runtime-complete receipt is never verified.
            self.assertEqual(summary["report"]["verified_attempts"], 0)
            self.assertEqual(summary["report"]["promotion"], "NOT_EVALUATED")
            self.assertEqual(
                summary["report"]["missing_identity_counts"]["traceparent"], 1
            )

    def test_malformed_ledger_is_fatal_not_partial(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = write_journal(directory, {"": []})
            ledger = directory / "ledger.jsonl"
            bad = ledger_record()
            del bad["costs"]["review"]["cost_usd"]
            ledger.write_text(json.dumps(bad) + "\n", encoding="utf-8")
            with self.assertRaises(edr.ReportError):
                run(live, ledger=ledger)

    def test_missing_ledger_file_is_fatal(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = write_journal(directory, {"": []})
            with self.assertRaises(edr.ReportError):
                run(live, ledger=directory / "nope.jsonl")


# ── CLI ──────────────────────────────────────────────────────────────────────


class TestCli(unittest.TestCase):
    def test_writes_json_to_output_path(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = write_journal(directory, {"": [envelope(eid="a", data=activity())]})
            out = directory / "report.json"
            code = edr.main(
                [
                    "--since",
                    "2026-09-05T00:00:00Z",
                    "--until",
                    "2026-09-06T00:00:00Z",
                    "--journal",
                    str(live),
                    "--db",
                    str(directory / "missing.db"),
                    "--output",
                    str(out),
                ]
            )
            self.assertEqual(code, 0)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["version"], edr.REPORT_VERSION)
            self.assertEqual(report["journal"]["records"]["unique"], 1)

    def test_bad_window_exits_nonzero_without_output(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            live = write_journal(directory, {"": []})
            out = directory / "report.json"
            code = edr.main(
                [
                    "--since",
                    "2026-09-05T00:00:00",  # no offset
                    "--journal",
                    str(live),
                    "--output",
                    str(out),
                ]
            )
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()


def test_repeated_collision_variant_is_still_deduplicated():
    first = envelope(eid="shared", time="2026-09-05T10:00:00Z")
    second = envelope(eid="shared", time="2026-09-05T11:00:00Z")
    with TemporaryDirectory() as tmp:
        live = write_journal(Path(tmp), {"1": [first, second], "": [second, first]})
        records = run(live)["journal"]["records"]
        assert records["unique"] == 2
        assert records["exact_duplicates_collapsed"] == 2
        assert records["id_collisions_kept"] == 1


def test_slug_cannot_override_missing_or_conflicting_absolute_identity():
    workers = {"/worktrees/project-a/shared": [("run", "task", "dispatch", "bead")]}
    report = edr.correlate_segments_to_workers([
        {"worktree": "/worktrees/project-b/shared", "worktree_slug": "shared"},
        {"worktree": None, "worktree_slug": "shared"},
    ], workers)
    assert report["unmatched"] == 2
    assert report["matched_unique"] == 0
