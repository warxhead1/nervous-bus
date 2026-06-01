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
        s.redis_client.xadd.return_value = "1-0"
        s.events_mirrored = 0
        s.events_dropped = 0
        s.redis_errors = 0
        return s

    def test_mirror_all_sends_unmatched_type_to_per_type_stream(self):
        s = self._make_state()
        raw = json.dumps({"type": "tengine.session.fps_drop", "data": {}})
        result = mirror.mirror_event(s, raw)
        self.assertTrue(result)
        calls = [c[0][0] for c in s.redis_client.xadd.call_args_list]
        self.assertIn("nbus:tengine.session.fps_drop", calls)

    def test_mirror_all_also_xadds_to_universal_stream(self):
        s = self._make_state()
        raw = json.dumps({"type": "tengine.session.fps_drop", "data": {}})
        mirror.mirror_event(s, raw)
        calls = [c[0][0] for c in s.redis_client.xadd.call_args_list]
        self.assertIn("nbus:all", calls)

    def test_mirror_all_false_skips_unmatched(self):
        s = self._make_state(mirror_all=False)
        raw = json.dumps({"type": "tengine.session.fps_drop", "data": {}})
        result = mirror.mirror_event(s, raw)
        self.assertFalse(result)

    def test_no_universal_stream_skips_nbus_all(self):
        s = self._make_state(universal_stream="")
        raw = json.dumps({"type": "bus.bead.created", "data": {}})
        mirror.mirror_event(s, raw)
        calls = [c[0][0] for c in s.redis_client.xadd.call_args_list]
        self.assertNotIn("nbus:all", calls)

if __name__ == "__main__":
    unittest.main()
