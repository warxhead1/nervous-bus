import sys, unittest
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0, str(Path(__file__).parent))
from calibration import (
    CalibrationState, should_auto_file, record_verdict, AUTO_ENABLE_VERDICTS,
    AUTO_ENABLE_ACCEPT_RATE, AUTO_ENABLE_NO_RECENT_REJECTS,
)

class TestCalibration(unittest.TestCase):
    def test_new_type_not_auto_enabled(self):
        state = CalibrationState("anomaly", "")
        self.assertFalse(should_auto_file(state))

    def test_auto_enables_after_threshold(self):
        state = CalibrationState("anomaly", "")
        state.accept_count = AUTO_ENABLE_VERDICTS
        state.reject_count = 0
        state.last_reject_idx = 0
        self.assertTrue(should_auto_file(state))

    def test_reject_rate_too_high_stays_disabled(self):
        state = CalibrationState("anomaly", "")
        state.accept_count = 15
        state.reject_count = 10
        self.assertFalse(should_auto_file(state))

    def test_recent_reject_blocks_auto_enable(self):
        state = CalibrationState("anomaly", "")
        state.accept_count = 20
        state.reject_count = 1
        state.last_reject_idx = 20  # reject was the last verdict
        self.assertFalse(should_auto_file(state))

    def test_record_accept_increments(self):
        state = CalibrationState("anomaly", "")
        record_verdict(state, "accept")
        self.assertEqual(state.accept_count, 1)

    def test_record_reject_updates_last_idx(self):
        state = CalibrationState("anomaly", "")
        record_verdict(state, "accept")
        record_verdict(state, "reject")
        total = state.accept_count + state.reject_count
        self.assertEqual(state.last_reject_idx, total)

class TestPerTypeThresholds(unittest.TestCase):
    def test_silence_never_auto_files(self):
        """silence requires 999 verdicts — effectively never auto-files."""
        state = CalibrationState("silence", "")
        state.accept_count = 998
        state.reject_count = 0
        state.last_reject_idx = 0
        self.assertFalse(should_auto_file(state))

    def test_recovery_auto_enables_faster(self):
        """recovery only needs 8 verdicts at 65% accept, 2 clean gap."""
        state = CalibrationState("recovery", "")
        state.accept_count = 8
        state.reject_count = 0
        state.last_reject_idx = 0
        self.assertTrue(should_auto_file(state))

    def test_anomaly_uses_lower_threshold_than_default(self):
        """anomaly needs 15 verdicts (not 20) on workstation config."""
        state = CalibrationState("anomaly", "")
        state.accept_count = 15
        state.reject_count = 0
        state.last_reject_idx = 0
        self.assertTrue(should_auto_file(state))

    def test_anomaly_still_blocked_at_14(self):
        state = CalibrationState("anomaly", "")
        state.accept_count = 14
        state.reject_count = 0
        state.last_reject_idx = 0
        self.assertFalse(should_auto_file(state))

    def test_recovery_blocked_by_recent_reject(self):
        """recovery clean_gap is 2 — reject in last 2 verdicts blocks it."""
        state = CalibrationState("recovery", "")
        state.accept_count = 8
        state.reject_count = 1
        state.last_reject_idx = 9  # just happened
        self.assertFalse(should_auto_file(state))

    def test_unknown_type_uses_anomaly_defaults(self):
        """Unknown signal types fall back to anomaly thresholds."""
        state = CalibrationState("correlation", "")
        state.accept_count = 15
        state.reject_count = 0
        state.last_reject_idx = 0
        self.assertTrue(should_auto_file(state))


if __name__ == "__main__":
    unittest.main()