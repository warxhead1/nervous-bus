#!/usr/bin/env python3
import json, sys, unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch
sys.path.insert(0, str(Path(__file__).parent))
import mirror

class TestMirrorAll(unittest.TestCase):
    def _make_state(self, mirror_all=True, universal_stream="nbus:all"):
        s = mirror.State.__new__(mirror.State)
        s.channel_prefixes = ["bus.bead"]
        s.mirror_all = mirror_all
        s.universal_stream = universal_stream
        s.universal_stream_maxlen = 50000
        s.maxlen = 10000
        s.trim_strategy = "MAXLEN"
        s.min_idle_ms = 0
        s.redis_connected = True
        s.redis_client = MagicMock()
        s.redis_client.eval.return_value = 1
        s.events_mirrored = 0
        s.events_dropped = 0
        s.events_deduped = 0
        s.redis_errors = 0
        return s

    def test_mirror_all_sends_unmatched_type_to_per_type_stream(self):
        s = self._make_state()
        raw = json.dumps({"type": "tengine.session.fps_drop", "data": {}})
        result = mirror.mirror_event(s, raw)
        self.assertTrue(result)
        args = s.redis_client.eval.call_args.args
        self.assertEqual(args[3], "nbus:tengine.session.fps_drop")

    def test_mirror_all_also_xadds_to_universal_stream(self):
        s = self._make_state()
        raw = json.dumps({"type": "tengine.session.fps_drop", "data": {}})
        mirror.mirror_event(s, raw)
        args = s.redis_client.eval.call_args.args
        self.assertEqual(args[4], "nbus:all")

    def test_mirror_all_false_skips_unmatched(self):
        s = self._make_state(mirror_all=False)
        raw = json.dumps({"type": "tengine.session.fps_drop", "data": {}})
        result = mirror.mirror_event(s, raw)
        self.assertFalse(result)

    def test_no_universal_stream_skips_nbus_all(self):
        s = self._make_state(universal_stream="")
        raw = json.dumps({"type": "bus.bead.created", "data": {}})
        mirror.mirror_event(s, raw)
        args = s.redis_client.eval.call_args.args
        self.assertEqual(args[4], "")


class TestIdempotentDelivery(unittest.TestCase):
    """An event whose id was already delivered by the live publish path (its
    dedup key exists) must be skipped here — no duplicate stream entry."""

    def _make_state(self):
        s = mirror.State.__new__(mirror.State)
        s.channel_prefixes = ["funsearch."]
        s.mirror_all = False
        s.universal_stream = "nbus:all"
        s.universal_stream_maxlen = 50000
        s.maxlen = 10000
        s.trim_strategy = "MAXLEN"
        s.min_idle_ms = 0
        s.redis_connected = True
        s.redis_client = MagicMock()
        s.redis_client.eval.return_value = 1
        s.events_mirrored = 0
        s.events_dropped = 0
        s.events_deduped = 0
        s.redis_errors = 0
        return s

    def test_already_claimed_event_is_skipped(self):
        s = self._make_state()
        s.redis_client.eval.return_value = 0
        raw = json.dumps({"id": "01ABC", "type": "funsearch.dedup_probe.v1", "data": {}})
        result = mirror.mirror_event(s, raw)
        self.assertFalse(result)
        s.redis_client.eval.assert_called_once()
        self.assertEqual(s.events_deduped, 1)

    def test_fresh_event_is_mirrored_and_claims_key(self):
        s = self._make_state()
        s.redis_client.eval.return_value = 1
        raw = json.dumps({"id": "01XYZ", "type": "funsearch.dedup_probe.v1", "data": {}})
        result = mirror.mirror_event(s, raw)
        self.assertTrue(result)
        args = s.redis_client.eval.call_args.args
        self.assertEqual(args[2], "nbus:dedup:01XYZ")
        self.assertEqual(args[3], "nbus:funsearch.dedup_probe.v1")
        self.assertEqual(s.events_deduped, 0)

    def test_atomic_publish_error_keeps_event_for_retry(self):
        s = self._make_state()
        s.redis_client.eval.side_effect = mirror.redis.RedisError("boom")
        raw = json.dumps({"id": "01ERR", "type": "funsearch.dedup_probe.v1", "data": {}})
        result = mirror.mirror_event(s, raw)
        self.assertFalse(result)
        self.assertEqual(s.events_dropped, 1)
        self.assertEqual(s.events_deduped, 0)


class TestUnknownChannelMetric(unittest.TestCase):
    """Unknown channels (no registered schema) are counted + passed through,
    never dropped or hard dead-lettered (kernel-unification spec §4)."""

    def _registry(self):
        reg = mirror.SchemaRegistry.__new__(mirror.SchemaRegistry)
        reg._registry = {}
        reg._envelope_types = set()
        reg.unknown_channels = {}
        return reg

    def test_unknown_channel_counted_and_passes_through(self):
        reg = self._registry()
        # validate() returns None (pass-through) for an unknown channel ...
        self.assertIsNone(reg.validate("sph.kernel.started.v1", {"data": {}}))
        # ... and tallies it.
        self.assertEqual(reg.unknown_channels["sph.kernel.started.v1"], 1)

    def test_unknown_channel_tally_increments(self):
        reg = self._registry()
        for _ in range(3):
            reg.validate("tsp.generation.completed.v1", {"data": {}})
        self.assertEqual(reg.unknown_channels["tsp.generation.completed.v1"], 3)


if __name__ == "__main__":
    unittest.main()


class TestRotationKeepsUnreadTail(unittest.TestCase):
    """Lines appended to the old file after the last read, then renamed away,
    must still be delivered before the reader moves to the new file."""

    def _reader(self, log, tmp):
        s = mirror.State.__new__(mirror.State)
        s.log_path = log
        s.offset_file = Path(tmp) / "offset.json"
        s.fp = None
        s.inode = None
        s.offset = 0
        return mirror.TailReader(s)

    def test_rename_then_recreate_delivers_old_tail_first(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "debug.jsonl"
            log.write_text('{"n":1}\n')
            r = self._reader(log, tmp)
            r.read_new_lines()
            self.assertEqual(r.read_new_lines(), ['{"n":1}'])
            with log.open("a") as fh:
                fh.write('{"n":2}\n')
            log.rename(log.with_suffix(".jsonl.1"))
            log.write_text('{"n":3}\n')
            self.assertEqual(r.read_new_lines(), ['{"n":2}', '{"n":3}'])
            self.assertEqual(r.read_new_lines(), [])

    def test_rename_before_recreate_delivers_old_tail(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "debug.jsonl"
            log.write_text("")
            r = self._reader(log, tmp)
            r.read_new_lines()
            with log.open("a") as fh:
                fh.write('{"n":1}\n')
            log.rename(log.with_suffix(".jsonl.1"))
            self.assertEqual(r.read_new_lines(), ['{"n":1}'])
            log.write_text('{"n":2}\n')
            r.read_new_lines()
            self.assertEqual(r.read_new_lines(), ['{"n":2}'])


def _big_line(n_bytes: int, event_type: str = "tengine.test.blob.v1") -> str:
    env = {"specversion": "1.0", "id": "01BIG", "source": "/tengine", "type": event_type,
           "time": "2026-11-14T00:00:00Z", "data": {"blob": ""}}
    pad = n_bytes - len(json.dumps(env))
    env["data"]["blob"] = "x" * pad
    return json.dumps(env)


class TestPayloadByteGuard(unittest.TestCase):
    """debug.jsonl lines bypass the SDK guard, so the mirror applies the same byte limit."""

    def _make_state(self, limit=16384):
        s = mirror.State.__new__(mirror.State)
        s.channel_prefixes = ["tengine."]
        s.mirror_all = False
        s.universal_stream = "nbus:all"
        s.universal_stream_maxlen = 50000
        s.maxlen = 10000
        s.trim_strategy = "MAXLEN"
        s.min_idle_ms = 0
        s.max_payload_bytes = limit
        s.redis_connected = True
        s.redis_client = MagicMock()
        s.redis_client.eval.return_value = 1
        s.events_mirrored = s.events_dropped = s.events_deduped = 0
        s.events_too_large = s.redis_errors = 0
        return s

    def test_oversize_line_is_dead_lettered_not_mirrored(self):
        s = self._make_state()
        raw = _big_line(20 * 1024)
        self.assertFalse(mirror.mirror_event(s, raw))
        s.redis_client.eval.assert_not_called()
        streams = [c.args[0] for c in s.redis_client.xadd.call_args_list]
        self.assertEqual(streams, ["nbus:bus.dead_letter", "nbus:all"])
        dl = json.loads(s.redis_client.xadd.call_args_list[0].args[1]["_raw"])
        self.assertEqual(dl["data"]["failure_reason"], "payload_too_large")
        self.assertEqual(dl["data"]["payload_bytes"], len(raw.encode()))
        self.assertEqual(dl["data"]["limit_bytes"], 16384)
        self.assertEqual(dl["data"]["original_type"], "tengine.test.blob.v1")
        self.assertEqual((s.events_too_large, s.events_dropped, s.events_mirrored), (1, 1, 0))

    def test_line_at_the_limit_is_mirrored(self):
        s = self._make_state()
        raw = _big_line(16384)
        self.assertEqual(len(raw.encode()), 16384)
        self.assertTrue(mirror.mirror_event(s, raw))
        s.redis_client.xadd.assert_not_called()

    def test_limit_counts_utf8_bytes_not_characters(self):
        s = self._make_state(limit=1000)
        env = json.loads(_big_line(200))
        env["data"]["blob"] = "é" * 600
        raw = json.dumps(env, ensure_ascii=False)
        self.assertLess(len(raw), 1000)
        self.assertFalse(mirror.mirror_event(s, raw))
        self.assertEqual(s.events_too_large, 1)

    def test_zero_limit_disables_the_check(self):
        s = self._make_state(limit=0)
        self.assertTrue(mirror.mirror_event(s, _big_line(20 * 1024)))

    def test_config_reads_the_limit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.toml"
            p.write_text("[streams]\nmax_payload_bytes = 4096\n")
            self.assertEqual(mirror.load_config(p)["max_payload_bytes"], 4096)
            p.write_text("[streams]\nmaxlen = 5\n")
            self.assertEqual(mirror.load_config(p)["max_payload_bytes"], 16384)


class TestPayloadByteGuardRealRedis(unittest.TestCase):
    """End to end: a 20 KB line in debug.jsonl against a private redis-server."""

    def test_20kb_line_reaches_dead_letter_only(self):
        import shutil, socket, subprocess, tempfile, time
        server = shutil.which("redis-server") or shutil.which("valkey-server")
        if not server:
            self.skipTest("no redis-server")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.Popen(
                [server, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no",
                 "--dir", tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                client = mirror.redis.Redis(port=port, decode_responses=True)
                for _ in range(50):
                    try:
                        client.ping()
                        break
                    except mirror.redis.ConnectionError:
                        time.sleep(0.1)
                log = Path(tmp) / "debug.jsonl"
                small = json.dumps({"specversion": "1.0", "id": "01SMALL", "source": "/tengine",
                                    "type": "tengine.session.frame.v1", "time": "2026-11-14T00:00:00Z",
                                    "data": {"n": 1}})
                log.write_text(_big_line(20 * 1024) + "\n" + small + "\n")
                state = mirror.State(
                    log_path=log, channel_prefixes=["tengine."], redis_url=f"redis://127.0.0.1:{port}",
                    redis_password=None, redis_db=0, connect_timeout_s=2.0, maxlen=100,
                    trim_strategy="MAXLEN", min_idle_ms=0, offset_file=Path(tmp) / "off.json",
                    metrics_interval_s=60.0, mirror_all=False, universal_stream="nbus:all",
                    universal_stream_maxlen=100, max_payload_bytes=16384)
                reader = mirror.TailReader(state)
                reader.open_log()
                with patch.object(mirror.SchemaRegistry, "validate", return_value=None):
                    for line in reader.read_new_lines():
                        mirror.mirror_event(state, line)
                typed = client.xrange("nbus:tengine.session.frame.v1")
                self.assertEqual([e[1]["event_id"] for e in typed], ["01SMALL"])
                dls = client.xrange("nbus:bus.dead_letter")
                self.assertEqual(len(dls), 1)
                self.assertEqual(json.loads(dls[0][1]["_raw"])["data"]["failure_reason"], "payload_too_large")
                self.assertEqual(client.xlen("nbus:all"), 2)
                self.assertEqual(state.events_too_large, 1)
            finally:
                proc.terminate()
                proc.wait(timeout=10)
