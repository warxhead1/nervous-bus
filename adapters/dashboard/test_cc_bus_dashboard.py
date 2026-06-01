#!/usr/bin/env python3
"""Tests for cc-bus-dashboard panel rendering.

Regression coverage for nervous-bus-i3xx: panel_signals must not raise
NameError when filter_glob is set and no annotations match the filter.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "cc-bus-dashboard"


def _load_module():
    """Load cc-bus-dashboard (no .py extension) as an importable module."""
    spec = importlib.util.spec_from_loader(
        "cc_bus_dashboard",
        importlib.machinery.SourceFileLoader("cc_bus_dashboard", str(SRC)),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cc_bus_dashboard"] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


class TestPanelSignalsClustersBinding(unittest.TestCase):
    """Regression: panel_signals must bind `clusters` even when annotations
    are filtered down to zero by filter_glob (was causing NameError +
    3s restart loop with blank alt-screen).
    """

    def _make_state(self, annotations, filter_glob=""):
        # build a minimal State; Redis is None so the body falls through
        # to using the test-provided annotations via monkeypatch
        st = _mod.State(log_path=Path("/dev/null"))
        st.filter_glob = filter_glob

        # Provide a fake Redis-like object whose hgetall returns the test
        # annotations encoded as JSON values.
        import json

        class _FakeRedis:
            def __init__(self, payload):
                self._payload = payload

            def hgetall(self, key):
                return {str(i).encode(): json.dumps(a).encode()
                        for i, a in enumerate(self._payload)}

        st._redis = _FakeRedis(annotations)
        return st

    def test_no_annotations_does_not_raise(self):
        state = self._make_state([])
        # Should render placeholder without error
        panel = _mod.panel_signals(state)
        self.assertIsNotNone(panel)

    def test_filter_glob_with_no_matches_does_not_raise(self):
        """Regression for nervous-bus-i3xx: filter_glob filters to empty
        but the cluster-building loop is still entered → NameError on
        `clusters`. Must not raise.
        """
        annotations = [
            {
                "signal_type": "anomaly",
                "confidence": 0.9,
                "channels": ["deer-flow.thread.created"],
                "description": "test signal",
                "severity": "info",
                "ts": 1_700_000_000,
            },
        ]
        state = self._make_state(annotations, filter_glob="hearth.*")
        # Before fix: NameError: name 'clusters' is not defined
        panel = _mod.panel_signals(state)
        self.assertIsNotNone(panel)

    def test_filter_glob_with_matches_renders(self):
        annotations = [
            {
                "signal_type": "pattern",
                "confidence": 0.7,
                "channels": ["hearth.ember.tick"],
                "description": "match",
                "severity": "warn",
                "ts": 1_700_000_000,
            },
        ]
        state = self._make_state(annotations, filter_glob="hearth.*")
        panel = _mod.panel_signals(state)
        self.assertIsNotNone(panel)


class TestEscAltSequenceDrain(unittest.TestCase):
    """Regression for keybind isolation (2026-05-16): ESC byte followed by a
    follow-up byte within 50ms is an Alt+X / CSI sequence — must drain &
    swallow, NOT quit. Bare ESC (no follow-up) still quits.

    Background: zellij `locked` mode forwards un-bound Alt+X keys as raw
    `\\x1b X` byte pairs to the pane. Before this fix, the dashboard's raw
    stdin reader treated any ESC as quit, so a stray Alt+[ in a bus-tab
    pane killed it — and the lifecycle wrapper `exec zsh` fallthrough left
    a dead shell pane that wouldn't re-promote.
    """

    def test_esc_alone_returns_false_quit(self):
        """Bare ESC (no follow-up byte) → caller should quit."""
        # poll_fn returns False — nothing else readable
        result = _mod._esc_is_alt_sequence(
            poll_fn=lambda t: False,
            read_fn=lambda n: b"",  # never called
        )
        self.assertFalse(result, "bare ESC must signal quit")

    def test_esc_with_followup_byte_returns_true_swallow(self):
        """ESC followed by another byte → Alt/CSI sequence, drain & swallow."""
        read_calls = []

        def fake_read(n):
            read_calls.append(n)
            return b"["  # representative Alt+[ payload

        result = _mod._esc_is_alt_sequence(
            poll_fn=lambda t: True,  # follow-up byte is ready
            read_fn=fake_read,
        )
        self.assertTrue(result, "Alt-sequence ESC must NOT signal quit")
        self.assertEqual(len(read_calls), 1, "must call read_fn exactly once to drain")
        self.assertEqual(read_calls[0], 8, "must drain up to 8 bytes (full CSI cap)")

    def test_esc_with_followup_csi_long_sequence(self):
        """ESC `[A` (arrow-up CSI) — drain should consume the trailer."""
        result = _mod._esc_is_alt_sequence(
            poll_fn=lambda t: True,
            read_fn=lambda n: b"[A",
        )
        self.assertTrue(result)

    def test_esc_drain_oserror_does_not_quit(self):
        """If draining raises OSError (closed fd), still treat as swallow."""
        def boom(n):
            raise OSError("fd closed")

        result = _mod._esc_is_alt_sequence(
            poll_fn=lambda t: True,
            read_fn=boom,
        )
        self.assertTrue(result, "OSError during drain must not propagate as quit")

    def test_poll_timeout_passed_to_poll_fn(self):
        """Default poll_timeout=0.05s — verify it reaches poll_fn."""
        seen = {}

        def spy_poll(t):
            seen["timeout"] = t
            return False

        _mod._esc_is_alt_sequence(poll_fn=spy_poll, read_fn=lambda n: b"")
        self.assertEqual(seen["timeout"], 0.05)


if __name__ == "__main__":
    unittest.main()
