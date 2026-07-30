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
