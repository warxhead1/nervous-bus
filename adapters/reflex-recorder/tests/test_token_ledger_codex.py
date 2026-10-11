"""token_ledger codex: rollout jsonl -> requests, deduped, cross-root, incremental."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import token_ledger as tl  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "codex_rollout_synthetic.jsonl"
UUID = "11111111-2222-3333-4444-555555555555"
NAME = f"rollout-2026-10-01T00-00-00-{UUID}.jsonl"


def _place(root: Path, day: str = "2026/10/01", name: str = NAME) -> Path:
    p = root / day / name
    p.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX, p)
    return p


def _tc(ts, total, last):
    keys = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    mk = lambda v: dict(zip(keys, v), total_tokens=v[0] + v[2])  # noqa: E731
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
        "type": "token_count", "info": {"total_token_usage": mk(total), "last_token_usage": mk(last)}}})


class TestCodexLedger(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.db = self.root / "tokens.db"

    def tearDown(self):
        self.td.cleanup()

    def _run(self, *codex_roots):
        return tl.run(self.db, roots=(), codex_roots=codex_roots)

    def test_dedupe_null_info_and_token_mapping(self):
        a = self.root / "a"
        _place(a)
        stats = self._run(a)
        # 3 token_count events: one info:null, one identical-total repeat -> 2 requests
        self.assertEqual(stats["requests_added"], 2)
        c = tl.connect(self.db)
        rows = c.execute("SELECT seq, model, input, cache_read, cache_create, cache_1h, cache_5m,"
                         " output, thinking, n_tool_uses FROM requests ORDER BY seq").fetchall()
        self.assertEqual(rows, [(1, "gpt-fake-1", 200, 800, 0, 0, 0, 50, 20, 2),
                                (2, "gpt-fake-1", 300, 1700, 0, 0, 0, 40, 10, 0)])
        s = c.execute("SELECT source, session_id, project, is_subagent FROM sessions").fetchone()
        self.assertEqual(s, ("codex", UUID, "demo", 0))

    def test_tool_call_mapping(self):
        a = self.root / "a"
        _place(a)
        self._run(a)
        c = tl.connect(self.db)
        rows = {r[0]: r[1:] for r in c.execute(
            "SELECT id, tool, cmd_class, cmd, result_chars FROM tool_calls")}
        self.assertEqual(rows["call_a"], ("exec_command", "test", "cargo test -p demo", 10))
        self.assertEqual(rows["call_b"], ("apply_patch", None, None, None))

    def test_incremental_append_and_state_carry_over(self):
        a = self.root / "a"
        p = _place(a)
        self._run(a)
        self.assertEqual(self._run(a)["requests_added"], 0)
        with open(p, "a") as f:
            # same total as the last event already ingested: must still be deduped
            f.write(_tc("2026-10-01T00:00:08.000Z", (3000, 2500, 90, 30), (2000, 1700, 40, 10)) + "\n")
            f.write(_tc("2026-10-01T00:00:09.000Z", (3500, 2900, 99, 31), (500, 400, 9, 1)) + "\n")
            f.write('{"timestamp":"partial')  # torn tail must not be consumed
        self.assertEqual(self._run(a)["requests_added"], 1)
        c = tl.connect(self.db)
        last = c.execute("SELECT seq, model, input, cache_read FROM requests ORDER BY seq DESC").fetchone()
        self.assertEqual(last, (3, "gpt-fake-1", 100, 400))  # model survived the chunk boundary
        self.assertEqual(self._run(a)["requests_added"], 0)

    def test_cross_root_dedupe_by_session_uuid(self):
        a, b = self.root / "codex" / "sessions", self.root / "orca" / "h" / "sessions"
        _place(a)
        _place(b, day="2026/10/02")  # same uuid, different day dir: still one session
        _place(b, name=f"rollout-2026-10-03T00-00-00-99999999-2222-3333-4444-555555555555.jsonl")
        info: dict = {}
        found = tl.discover_codex((a, b), info)
        self.assertEqual(len(found), 2)
        self.assertEqual(info, {"files_seen": 3, "duplicate_files": 1})
        stats = self._run(a, b)
        self.assertEqual(stats["codex_duplicate_files"], 1)
        self.assertEqual(stats["requests_added"], 4)  # 2 sessions x 2, the duplicate counted once
        c = tl.connect(self.db)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)

    def test_largest_copy_wins(self):
        a, b = self.root / "a", self.root / "b"
        _place(a)
        pb = _place(b)
        with open(pb, "a") as f:
            f.write(_tc("2026-10-01T00:00:08.000Z", (3500, 2900, 99, 31), (500, 400, 9, 1)) + "\n")
        self.assertEqual(self._run(a, b)["requests_added"], 3)

    def test_subagent_flag(self):
        a = self.root / "a"
        p = _place(a)
        txt = p.read_text().replace('"thread_source":"user"', '"thread_source":"subagent"')
        p.write_text(txt)
        self._run(a)
        c = tl.connect(self.db)
        self.assertEqual(c.execute("SELECT is_subagent FROM sessions").fetchone()[0], 1)


class TestReportParity(unittest.TestCase):
    """token_report groups by source; the cache-TTL net is null for no-cache-write sources."""

    BASE_KEYS = {"window_days", "since", "generated_at", "weights", "totals", "total_iet",
                 "candidates", "iet_per_commit"}

    def setUp(self):
        from datetime import datetime, timedelta, timezone
        self.td = tempfile.TemporaryDirectory()
        self.db = Path(self.td.name) / "tokens.db"
        self.c = tl.connect(self.db)
        mk = lambda m: (datetime.now(timezone.utc) - timedelta(minutes=m)).strftime(  # noqa: E731
            "%Y-%m-%dT%H:%M:%S.000Z")
        self.mk = mk
        self.c.execute("INSERT INTO sessions (key, source, project, is_subagent, started)"
                       " VALUES ('c1','claude','p',1,?)", (mk(60),))
        for seq, ago, cr, cc in ((1, 60, 0, 50_000), (2, 40, 0, 151_000)):
            self.c.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (f"c1#{seq}", "c1", seq, mk(ago), "m", 1, cr, cc, 0, cc, 10, 0, 0))
        self.c.commit()

    def tearDown(self):
        self.td.cleanup()

    def _add(self, source, sub=1):
        self.c.execute("INSERT INTO sessions (key, source, project, is_subagent, started)"
                       " VALUES (?,?,?,?,?)", (f"{source}:1", source, "p", sub, self.mk(30)))
        for seq, ago in ((1, 30), (2, 20)):  # 10-minute idle gap, context grew, zero cache writes
            self.c.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (f"{source}:1#{seq}", f"{source}:1", seq, self.mk(ago), "m",
                            30_000 * seq, 90_000, 0, 0, 0, 100, 40, 1))
        self.c.commit()

    def test_claude_only_ledger_output_is_unchanged(self):
        import token_report as tr
        rep = tr.build(self.c, 7)
        self.assertEqual(set(rep), self.BASE_KEYS)
        self.assertNotIn("Agent parity", tr.to_markdown(rep))
        self.assertEqual([c["id"] for c in rep["candidates"]].count("cache_rewrites"), 1)

    def test_mixed_ledger_adds_by_source_and_keeps_claude_rows(self):
        import token_report as tr
        before = {t["model"]: t for t in tr.build(self.c, 7)["totals"] if t["source"] == "claude"}
        self._add("codex")
        self._add("gemini", sub=0)
        rep = tr.build(self.c, 7)
        self.assertEqual(set(rep), self.BASE_KEYS | {"by_source"})
        self.assertEqual({t["model"]: t for t in rep["totals"] if t["source"] == "claude"}, before)
        bs = {r["source"]: r for r in rep["by_source"]}
        self.assertEqual(set(bs), {"claude", "codex", "gemini"})
        self.assertEqual(bs["codex"]["requests"], 2)
        self.assertEqual(bs["codex"]["thinking"], 80)
        for src in ("codex", "gemini"):
            self.assertIsNone(bs[src]["ttl_net_if_1h_iet"])  # null, not 0 and not a saving
            self.assertIn("no cache-write", bs[src]["ttl_note"])
        self.assertIsInstance(bs["claude"]["ttl_net_if_1h_iet"], int)
        # a zero-cache-write subagent source must not appear in the per-agent TTL table
        ttl = [d for c in rep["candidates"] if c["id"] == "cache_rewrites"
               for d in c["detail"]["ttl_by_agent_type"]]
        self.assertEqual({r["source"] for r in ttl}, {"claude"})
        md = tr.to_markdown(rep)
        self.assertIn("Agent parity", md)
        self.assertIn("n/a (no cache writes)", md)
        json.dumps(rep)  # serialisable with the null in place


if __name__ == "__main__":
    unittest.main()
