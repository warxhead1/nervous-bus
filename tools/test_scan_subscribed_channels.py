"""Tests for scan_subscribed_channels.py.

Run with:
    python -m pytest tools/test_scan_subscribed_channels.py -xvs
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCANNER = Path(__file__).parent / "scan_subscribed_channels.py"


def run_scanner(*paths) -> list[dict]:
    cmd = [sys.executable, str(SCANNER), *map(str, paths)]
    res = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(res.stdout)


def _by_channel(out: list[dict]) -> dict[str, str]:
    """channel → match_type (last wins; fine for assertions)."""
    return {r["channel"]: r["match_type"] for r in out}


# ─── Python ──────────────────────────────────────────────────────────────────


def test_python_glob_list_constant(tmp_path: Path) -> None:
    """`_CHANNEL_GLOBS = [...]` list literal of globs → prefix subs."""
    fixture = tmp_path / "collector.py"
    fixture.write_text(
        '''
_CHANNEL_GLOBS = [
    "autobench.*",
    "deer-flow.guidance.*",
    "bus.bead.*",
    "deer-flow.forge.*",
    "hearth-loom.ac.verified.*",
    "loomie.bead.checkpoint.*",
]
'''
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    # Trailing `.*` stripped, recorded as prefix.
    assert bc.get("autobench") == "prefix"
    assert bc.get("deer-flow.guidance") == "prefix"
    assert bc.get("bus.bead") == "prefix"
    assert bc.get("deer-flow.forge") == "prefix"
    assert bc.get("hearth-loom.ac.verified") == "prefix"
    assert bc.get("loomie.bead.checkpoint") == "prefix"


def test_python_single_segment_prefix_not_dropped(tmp_path: Path) -> None:
    """`autobench.*` → `autobench` (single segment) must NOT be filtered out."""
    fixture = tmp_path / "a.py"
    fixture.write_text('_GLOB = "autobench.*"\n')
    out = run_scanner(fixture)
    bc = _by_channel(out)
    assert bc.get("autobench") == "prefix"


def test_python_exact_channel_constant(tmp_path: Path) -> None:
    """Bare `CHANNEL` and `_CHANNEL` constants → exact subs."""
    fixture = tmp_path / "c.py"
    fixture.write_text(
        '''
class Router:
    CHANNEL = "bus.hearth.bead.changes.v1"

_CHANNEL = "deer-flow.guidance.fact.v1"
'''
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    assert bc.get("bus.hearth.bead.changes.v1") == "exact"
    assert bc.get("deer-flow.guidance.fact.v1") == "exact"


def test_python_subscribe_call_resolves_constant(tmp_path: Path) -> None:
    """`subscribe(channel_glob=_BUS_BEAD_GLOB)` resolves the Name constant."""
    fixture = tmp_path / "forge.py"
    fixture.write_text(
        '''
_BUS_BEAD_GLOB = "bus.bead.*"

async def start(self):
    self._sub = await self._broker.subscribe(channel_glob=_BUS_BEAD_GLOB)
'''
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    assert bc.get("bus.bead") == "prefix"


def test_python_subscribe_call_string_literal(tmp_path: Path) -> None:
    fixture = tmp_path / "d.py"
    fixture.write_text(
        'await broker.subscribe(channel_glob="deer-flow.research.dispatch.v1")\n'
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    assert bc.get("deer-flow.research.dispatch.v1") == "exact"


def test_python_subscribe_self_attribute(tmp_path: Path) -> None:
    """subscribe(channel_glob=self.CHANNEL) resolves the class constant."""
    fixture = tmp_path / "e.py"
    fixture.write_text(
        '''
class R:
    CHANNEL = "bus.hearth.bead.changes.v1"
    async def start(self):
        await self._broker.subscribe(channel_glob=self.CHANNEL)
'''
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    assert bc.get("bus.hearth.bead.changes.v1") == "exact"


# ─── Rust ────────────────────────────────────────────────────────────────────


def test_rust_starts_with_and_eq(tmp_path: Path) -> None:
    fixture = tmp_path / "consumer.rs"
    fixture.write_text(
        '''
fn event_type_matches(&self) -> bool {
    let t = &self.event_type;
    t.starts_with("bus.bead.")
        || t == "agent.session"
        || t == "bus.dead_letter"
        || t.starts_with("tengine.")
        || t.starts_with("deer-flow.forge.")
        || t == "deer-flow.research.cycle.completed.v1"
        || t.starts_with("loom.lifecycle")
        || t == "turn_end"
}
'''
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    # starts_with → prefix (trailing dot stripped).
    assert bc.get("bus.bead") == "prefix"
    assert bc.get("tengine") == "prefix"
    assert bc.get("deer-flow.forge") == "prefix"
    assert bc.get("loom.lifecycle") == "prefix"
    # == → exact.
    assert bc.get("agent.session") == "exact"
    assert bc.get("bus.dead_letter") == "exact"
    assert bc.get("deer-flow.research.cycle.completed.v1") == "exact"
    # `turn_end` has no dot → filtered out entirely.
    assert "turn_end" not in bc


def test_rust_dispatch_chain_exacts(tmp_path: Path) -> None:
    fixture = tmp_path / "dispatch.rs"
    fixture.write_text(
        '''
fn dispatch(t: &str) {
    if t.starts_with("deer-flow.cycle.snapshot") {
    } else if t.starts_with("deer-flow.run.") {
    } else if t == "loom.lifecycle.pr.v1" {
    } else if t == "bus.notify.v1" {
    }
}
'''
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    assert bc.get("deer-flow.cycle.snapshot") == "prefix"
    assert bc.get("deer-flow.run") == "prefix"
    assert bc.get("loom.lifecycle.pr.v1") == "exact"
    assert bc.get("bus.notify.v1") == "exact"


# ─── Go ──────────────────────────────────────────────────────────────────────


def test_go_newconsumer_string_list(tmp_path: Path) -> None:
    fixture = tmp_path / "polling.go"
    fixture.write_text(
        '''package main

func boot() {
    nbusConsumer = gateway.NewNbusConsumer(
        url,
        []string{"agent.session", "loom.lifecycle.v1"},
    )
}
'''
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    assert bc.get("agent.session") == "exact"
    assert bc.get("loom.lifecycle.v1") == "exact"


def test_go_nbus_stream_strip(tmp_path: Path) -> None:
    """`const X Stream = "nbus:<ch>"` → strip `nbus:` prefix."""
    fixture = tmp_path / "stream.go"
    fixture.write_text(
        '''package gateway

const (
    lifecycleStream = "nbus:loom.lifecycle.v1"
    activityStream  = "nbus:gateway.activity.v1"
)
const busAgentActivityStream = "nbus:bus.agent.activity.v1"
const nbusUniversalStream = "nbus:all"
'''
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    assert bc.get("loom.lifecycle.v1") == "exact"
    assert bc.get("gateway.activity.v1") == "exact"
    assert bc.get("bus.agent.activity.v1") == "exact"
    # `nbus:all` → `all` has no dot → filtered.
    assert "all" not in bc


def test_go_switch_case(tmp_path: Path) -> None:
    fixture = tmp_path / "consumer.go"
    fixture.write_text(
        '''package bus
func route(channel string) {
    switch channel {
    case "agent.session":
    case "loom.lifecycle.v1":
    case "json":
    }
}
'''
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    assert bc.get("agent.session") == "exact"
    assert bc.get("loom.lifecycle.v1") == "exact"
    assert "json" not in bc


# ─── TypeScript ──────────────────────────────────────────────────────────────


def test_ts_channels_map_resolution(tmp_path: Path) -> None:
    """CHANNELS map + onNbusEvent(CHANNELS.KEY) resolves to literal."""
    schemas = tmp_path / "schemas.ts"
    schemas.write_text(
        '''
export const CHANNELS = {
  COMMAND_HALT: 'tachyonos.command.halt.v1',
  TRADE_APPROVED: 'tachyonos.trade.approved.v1',
  COMMAND_RESEARCH_TICKER: 'tachyonos.command.research_ticker.v1',
} as const;
'''
    )
    instrumentation = tmp_path / "instrumentation.ts"
    instrumentation.write_text(
        '''
import { onNbusEvent } from './subscriber';
onNbusEvent(CHANNELS.COMMAND_HALT, async (event) => {});
onNbusEvent(CHANNELS.TRADE_APPROVED, async (event) => {});
onNbusEvent(CHANNELS.COMMAND_RESEARCH_TICKER, async (event) => {});
'''
    )
    out = run_scanner(tmp_path)
    bc = _by_channel(out)
    assert bc.get("tachyonos.command.halt.v1") == "exact"
    assert bc.get("tachyonos.trade.approved.v1") == "exact"
    assert bc.get("tachyonos.command.research_ticker.v1") == "exact"


def test_ts_direct_literal(tmp_path: Path) -> None:
    fixture = tmp_path / "direct.ts"
    fixture.write_text(
        "onNbusEvent('tachyonos.risk.circuit_breaker_tripped.v1', handler);\n"
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    assert bc.get("tachyonos.risk.circuit_breaker_tripped.v1") == "exact"


# ─── Annotation escape hatch ─────────────────────────────────────────────────


def test_annotation_python_comment(tmp_path: Path) -> None:
    fixture = tmp_path / "dyn.py"
    fixture.write_text(
        '''
def subscribe_dynamic(self):
    # nbus-sub: deer-flow.dynamic.foo.v1
    ch = compute_channel()
    self._broker.subscribe(channel_glob=ch)
'''
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    assert bc.get("deer-flow.dynamic.foo.v1") == "exact"


def test_annotation_slash_comment_prefix(tmp_path: Path) -> None:
    fixture = tmp_path / "dyn.go"
    fixture.write_text(
        '''package x
// nbus-sub: deer-flow.computed.*
func sub() {}
'''
    )
    out = run_scanner(fixture)
    bc = _by_channel(out)
    assert bc.get("deer-flow.computed") == "prefix"


# ─── Nested git repos / worktrees ────────────────────────────────────────────


def test_nested_git_worktree_not_scanned(tmp_path: Path) -> None:
    """A subdirectory that is itself a git repo/worktree (has its own `.git`)
    is a separate checkout boundary and must NOT be descended into, or its
    source is double-counted under both the parent walk and its own path.

    Covers both forms of `.git`: a directory (real repo) and a file (worktree
    pointer, as produced by `git worktree add` — the autoloom-owner-col case).
    """
    # Top-level source — should be scanned.
    (tmp_path / "top.py").write_text('_GLOB = "top.channel.*"\n')

    # Nested repo via a `.git` DIRECTORY.
    nested_dir = tmp_path / "nested-repo"
    nested_dir.mkdir()
    (nested_dir / ".git").mkdir()
    (nested_dir / "inner.py").write_text('_GLOB = "nested.repo.channel.*"\n')

    # Nested worktree via a `.git` FILE.
    nested_wt = tmp_path / "nested-worktree"
    nested_wt.mkdir()
    (nested_wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
    (nested_wt / "inner.py").write_text('_GLOB = "nested.worktree.channel.*"\n')

    out = run_scanner(tmp_path)
    channels = {r["channel"] for r in out}
    assert "top.channel" in channels
    # Neither nested checkout's sources should appear.
    assert "nested.repo.channel" not in channels, channels
    assert "nested.worktree.channel" not in channels, channels
    files = {r["file"] for r in out}
    assert not any(f.startswith("nested-repo/") for f in files), files
    assert not any(f.startswith("nested-worktree/") for f in files), files


# ─── Output format ───────────────────────────────────────────────────────────


def test_records_have_match_type_and_consumer(tmp_path: Path) -> None:
    fixture = tmp_path / "x.py"
    fixture.write_text('_GLOB = "bus.bead.*"\n_CHANNEL = "deer-flow.guidance.fact.v1"\n')
    out = run_scanner(f"my-consumer={tmp_path}")
    assert out
    for r in out:
        assert "match_type" in r and r["match_type"] in {"exact", "prefix"}
        assert r["consumer"] == "my-consumer"
        assert "file" in r and "line" in r and "channel" in r


def test_output_sorted_and_deduped(tmp_path: Path) -> None:
    fixture = tmp_path / "a.py"
    fixture.write_text('_GLOB = "bus.bead.*"\n_OTHER_GLOB = "autobench.*"\n')
    out = run_scanner(f"c={tmp_path}")
    seen: set[tuple] = set()
    for r in out:
        k = (r["consumer"], r["file"], r["line"], r["channel"], r["match_type"])
        assert k not in seen, f"duplicate: {k}"
        seen.add(k)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-xvs"]))
