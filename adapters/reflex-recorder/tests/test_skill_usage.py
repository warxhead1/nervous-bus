"""tests/test_skill_usage.py — skill-load / dispatch-tiering extraction, backfill, queries.

Fixtures mirror real bus.agent.activity.v1 shapes (names genericized). Never
touches the real runs.db.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).parent.parent
sys.path.insert(0, str(_HERE))

from skill_usage import (  # noqa: E402
    backfill,
    classify_root,
    extract_signals,
    features_from_events,
    fold_skill_features,
)
from query import (  # noqa: E402
    query_dispatch,
    query_skill_inventory,
    query_skill_lift,
    query_skills,
)
from store import SQLiteStore  # noqa: E402


def _call(tool, summary, **kw):
    return {"event": "tool_call", "tool_name": tool, "tool_summary": summary, **kw}


SKILL_TOOL = _call("Skill", '{"skill":"artifact-kit"}')
CODEX_SED = _call(
    "Bash",
    "sed -n '1,240p' /home/u/.agents/skills/orca-cli/SKILL.md\nbd prime\ngit status --short",
    agent_kind="codex-cli",
)
AGENT_TRUNCATED = _call(
    "Agent",
    '{"description":"L1 audit","isolation":"worktree","model":"sonnet","name":"L1",'
    '"prompt":"You are LANE L1 and this prompt was cut off mid-str',
)
AGENT_NO_MODEL = _call("Agent", json.dumps({"description": "d", "prompt": "p", "subagent_type": "Explore"}))
AGENT_TRUNC_NO_MODEL = _call("Agent", '{"description":"d","prompt":"long prompt cut')


class TestExtract(unittest.TestCase):
    def test_skill_tool(self):
        self.assertEqual(extract_signals(SKILL_TOOL),
                         [{"kind": "skill", "name": "artifact-kit", "mechanism": "skill_tool"}])

    def test_skill_tool_truncated_falls_back_to_regex(self):
        sig = extract_signals(_call("Skill", '{"args":"x","skill":"council"'))
        self.assertEqual(sig[0]["name"], "council")

    def test_codex_shell_read(self):
        sig = extract_signals(CODEX_SED)
        self.assertEqual(sig, [{"kind": "skill", "name": "orca-cli", "mechanism": "file_read", "root": "agents"}])

    def test_read_tool_plain_and_json_summary(self):
        a = extract_signals(_call("Read", "/work/skills/artifact-kit/SKILL.md"))
        b = extract_signals(_call("Read", '{"file_path":"/home/u/.claude/skills/artifact-kit/SKILL.md","limit":100}'))
        self.assertEqual((a[0]["name"], a[0]["root"]), ("artifact-kit", "project"))
        self.assertEqual((b[0]["name"], b[0]["root"]), ("artifact-kit", "claude"))

    def test_multiple_and_duplicate_refs_in_one_command(self):
        cmd = ("cat ~/.codex/skills/a/SKILL.md ~/.codex/skills/b/SKILL.md "
               "&& wc -l ~/.codex/skills/a/SKILL.md")
        self.assertEqual([s["name"] for s in extract_signals(_call("Bash", cmd))], ["a", "b"])

    def test_non_skill_md_paths_ignored(self):
        self.assertEqual(extract_signals(_call("Bash", "cat docs/skills/README.md; cat SKILL.md")), [])

    def test_tool_return_not_counted(self):
        self.assertEqual(extract_signals({**SKILL_TOOL, "event": "tool_return"}), [])

    def test_truncated_dispatch_recovers_model_but_subagent_unknown(self):
        (s,) = extract_signals(AGENT_TRUNCATED)
        self.assertEqual((s["model"], s["model_state"]), ("sonnet", "explicit"))
        self.assertEqual(s["subagent_type_state"], "unknown")
        self.assertEqual(s["isolation"], "worktree")

    def test_complete_json_without_model_is_missing(self):
        (s,) = extract_signals(AGENT_NO_MODEL)
        self.assertEqual(s["model_state"], "missing")
        self.assertEqual(s["subagent_type"], "Explore")

    def test_truncated_without_model_is_unknown_not_missing(self):
        (s,) = extract_signals(AGENT_TRUNC_NO_MODEL)
        self.assertEqual(s["model_state"], "unknown")

    def test_task_alias_and_workflow(self):
        self.assertEqual(extract_signals(_call("Task", AGENT_NO_MODEL["tool_summary"]))[0]["tool"], "Task")
        self.assertEqual(extract_signals(_call("Workflow", "{}")), [{"kind": "workflow"}])

    def test_classify_root(self):
        self.assertEqual(classify_root("/home/u/.claude/plugins/cache/x/skills/y/SKILL.md"), "plugin")
        self.assertEqual(classify_root("/home/u/projects/r/.claude/skills/y/SKILL.md"), "project")
        self.assertEqual(classify_root("/home/u/.codex/skills/y/SKILL.md"), "codex")


class TestFold(unittest.TestCase):
    def test_fold_aggregates(self):
        f: dict = {}
        for ev in (SKILL_TOOL, SKILL_TOOL, CODEX_SED, AGENT_TRUNCATED, AGENT_NO_MODEL,
                   AGENT_TRUNC_NO_MODEL, _call("Workflow", "{}")):
            fold_skill_features(f, ev)
        self.assertEqual(f["skills"], {"artifact-kit": {"skill_tool": 2}, "orca-cli": {"file_read": 1}})
        d = f["dispatch"]
        self.assertEqual((d["total"], d["model_missing"], d["model_unknown"], d["isolated"]), (3, 1, 1, 1))
        self.assertEqual(d["by_model"], {"sonnet": 1})
        self.assertEqual(d["by_subagent_type"], {"unknown": 2, "Explore": 1})
        self.assertEqual(f["workflow_calls"], 1)

    def test_extraction_error_never_raises(self):
        f: dict = {}
        fold_skill_features(f, {"event": "tool_call", "tool_name": "Skill", "tool_summary": object()})
        self.assertEqual(f.get("skill_extract_errors"), 1)


def _envelope(data: dict) -> str:
    return json.dumps({"type": "bus.agent.activity.v1", "data": data})


class _DBCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "runs.db"
        SQLiteStore(self.db)._conn.close()
        self.conn = sqlite3.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def add_run(self, run_id, project="p", agent_kind="host_claude_code", started="2026-09-01T00:00:00Z",
                outcome=None, features=None, events=()):
        self.conn.execute(
            "INSERT INTO runs (run_id, run_key, run_key_kind, project, agent_kind, started, ended,"
            " outcome, labeled_at, features, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, run_id, "session", project, agent_kind, started, started, outcome,
             started if outcome else None, json.dumps(features or {}), started),
        )
        for i, ev in enumerate(events):
            self.conn.execute(
                "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
                (run_id, i, started, "bus.agent.activity.v1", _envelope(ev)),
            )
        self.conn.commit()

    def ro(self):
        c = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c


class TestBackfill(_DBCase):
    def test_dry_run_writes_nothing_and_apply_is_idempotent(self):
        self.add_run("r1", features={"tool_calls": 5, "skills": {"stale": {"skill_tool": 9}}},
                     events=[SKILL_TOOL, {**SKILL_TOOL, "event": "tool_return"}, AGENT_NO_MODEL])
        self.add_run("r2", events=[CODEX_SED])

        totals = backfill(self.db, None, apply=False, out=io.StringIO())
        self.assertEqual(totals["updated"], 0)
        feats = json.loads(self.conn.execute("SELECT features FROM runs WHERE run_id='r1'").fetchone()[0])
        self.assertIn("stale", feats["skills"])
        self.assertEqual(totals["skill_by_mechanism"], {"skill_tool": 1, "file_read": 1})

        for _ in range(2):
            backfill(self.db, None, apply=True, out=io.StringIO())
        feats = json.loads(self.conn.execute("SELECT features FROM runs WHERE run_id='r1'").fetchone()[0])
        self.assertEqual(feats["skills"], {"artifact-kit": {"skill_tool": 1}})
        self.assertEqual(feats["tool_calls"], 5)  # unrelated features preserved
        self.assertEqual(feats["dispatch"]["model_missing"], 1)

    def test_since_filter(self):
        self.add_run("old", started="2026-01-01T00:00:00Z", events=[SKILL_TOOL])
        self.add_run("new", started="2026-09-01T00:00:00Z", events=[SKILL_TOOL])
        self.assertEqual(backfill(self.db, "2026-06-01", apply=False, out=io.StringIO())["runs"], 1)

    def test_features_from_events_skips_bad_json(self):
        self.assertEqual(features_from_events(["not json", _envelope(SKILL_TOOL)])["skills"],
                         {"artifact-kit": {"skill_tool": 1}})


class TestQueries(_DBCase):
    def test_skills_grouping_and_harness_split(self):
        self.add_run("a", project="p1", features={"skills": {"s": {"skill_tool": 2}}})
        self.add_run("b", project="p2", agent_kind="codex-cli", started="2026-08-01T00:00:00Z",
                     features={"skills": {"s": {"file_read": 1}}})
        (row,) = query_skills(self.ro())
        self.assertEqual((row["invocations"], row["runs"], row["projects"]), (3, 2, 2))
        self.assertEqual(row["harness"], {"host_claude_code": 2, "codex-cli": 1})
        self.assertEqual(row["first_seen"], "2026-08-01T00:00:00Z")
        by_month = query_skills(self.ro(), by="month")
        self.assertEqual(sorted(r["group"] for r in by_month), ["2026-08", "2026-09"])

    def test_inventory_never_seen_and_plugin_names(self):
        root = Path(self._tmp.name) / "skills"
        for name in ("used", "idle", "rescue"):
            (root / name).mkdir(parents=True)
            (root / name / "SKILL.md").write_text("---\nname: x\n---\n")
        self.add_run("a", features={"skills": {"used": {"skill_tool": 1}, "codex:rescue": {"skill_tool": 2},
                                               "ghost": {"file_read": 1}}})
        rows = {r["skill"]: r for r in query_skill_inventory(self.ro(), roots=[root])}
        self.assertEqual(rows["idle"]["status"], "never-seen")
        self.assertEqual((rows["rescue"]["status"], rows["rescue"]["invocations"]), ("used", 2))
        self.assertEqual(rows["ghost"]["status"], "not-on-disk")
        self.assertNotIn("codex:rescue", rows)

    def test_lift_same_stratum_labeled_only(self):
        for i in range(3):
            self.add_run(f"w{i}", project="p", outcome="clean", features={"skills": {"s": {"skill_tool": 1}}})
        for i, out in enumerate(["abandoned", "abandoned", "clean"]):
            self.add_run(f"o{i}", project="p", outcome=out)
        # other stratum without the skill must not enter the comparison arm
        for i in range(5):
            self.add_run(f"x{i}", project="other", outcome="abandoned")
        # unlabeled run with the skill must not count
        self.add_run("u", project="p", features={"skills": {"s": {"skill_tool": 1}}})
        (row,) = query_skill_lift(self.ro(), min_n=3)
        self.assertEqual((row["with_n"], row["without_n"]), (3, 3))
        self.assertEqual((row["with_good_rate"], row["without_good_rate"]), (1.0, 0.333))
        self.assertEqual(row["status"], "ok")
        self.assertEqual(query_skill_lift(self.ro(), min_n=4)[0]["status"], "insufficient")

    def test_dispatch_missing_rate_excludes_unknown(self):
        self.add_run("a", features={"dispatch": {"total": 4, "by_model": {"sonnet": 2}, "by_subagent_type": {},
                                                 "model_missing": 1, "model_unknown": 1, "isolated": 2}})
        (row,) = query_dispatch(self.ro())
        self.assertEqual(row["model_missing_rate"], round(1 / 3, 3))
        self.assertEqual(row["isolation_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
