"""Tests for scan_emitted_channels.py.

Run with:
    python -m pytest tools/test_scan_emitted_channels.py -xvs
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCANNER = Path(__file__).parent / "scan_emitted_channels.py"


def run_scanner(*paths) -> list[dict]:
    """Run the scanner against the given paths and return parsed JSON."""
    cmd = [sys.executable, str(SCANNER), *map(str, paths)]
    res = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(res.stdout)


# ─── Python ──────────────────────────────────────────────────────────────────


def test_python_publish_helpers(tmp_path: Path) -> None:
    fixture = tmp_path / "py_emit.py"
    fixture.write_text(
        '''
from nbus import publish
import nervous

def foo():
    nbus.publish("bus.bead.lifecycle.v1", {"a": 1})
    nervous.publish("deer-flow.research.finding.v1", {"q": 2})
    obs._publish("autobench.case.result.v1", {})

CHANNEL_LIFECYCLE = "loom.lifecycle.v1"
SOMETHING_STREAM = "bus.bead.created"
TRAILING_TOPIC = "agent.message.v1"

def bar():
    emit("deer-flow.semantic_cache.hit", {})
    # too short, single token — should NOT match
    emit("ok", {})
'''
    )
    out = run_scanner(fixture)
    channels = sorted({r["channel"] for r in out})
    assert "bus.bead.lifecycle.v1" in channels
    assert "deer-flow.research.finding.v1" in channels
    assert "autobench.case.result.v1" in channels
    assert "loom.lifecycle.v1" in channels
    assert "bus.bead.created" in channels
    assert "agent.message.v1" in channels
    assert "deer-flow.semantic_cache.hit" in channels
    assert "ok" not in channels

    # Every record carries emit_type.
    types = {r["emit_type"] for r in out}
    assert types <= {"const", "call"}
    # Constants should be tagged const; call sites tagged call.
    for r in out:
        if r["channel"] == "loom.lifecycle.v1":
            assert r["emit_type"] == "const"
        if r["channel"] == "bus.bead.lifecycle.v1":
            assert r["emit_type"] == "call"


def test_python_skips_dynamic_channels(tmp_path: Path) -> None:
    fixture = tmp_path / "dyn.py"
    fixture.write_text(
        """
def foo(channel):
    nbus.publish(channel, {})         # no static channel → skip
    nbus.publish(f"prefix.{x}", {})    # f-string → skip
"""
    )
    out = run_scanner(fixture)
    assert out == []


def test_python_records_have_line_numbers(tmp_path: Path) -> None:
    fixture = tmp_path / "lines.py"
    fixture.write_text(
        """# line 1
# line 2
nbus.publish("bus.dashboard.v1", {})
"""
    )
    out = run_scanner(fixture)
    assert len(out) == 1
    assert out[0]["line"] == 3
    assert out[0]["channel"] == "bus.dashboard.v1"
    assert out[0]["emit_type"] == "call"


# ─── Go ──────────────────────────────────────────────────────────────────────


def test_go_publish_call(tmp_path: Path) -> None:
    fixture = tmp_path / "pub.go"
    fixture.write_text(
        '''package bus

const BeadLifecycleStream = "bead.lifecycle.v1"
const LoomCoordStream = "loom.coord"
const Other = "not-a-channel-without-dots"

func emit(ctx Context, p *Publisher) {
    p.Publish(ctx, "exec.lifecycle.v1", evt)
    p.Publish(ctx, BeadLifecycleStream, evt) // const ref — not extracted directly
}

func shellOut() {
    exec.Command("nervous", "publish", "agent.message.v1", payload)
    exec.CommandContext(ctx, "nervous", "publish", "agent.session", data)
}
'''
    )
    out = run_scanner(fixture)
    channels = sorted({r["channel"] for r in out})
    assert "bead.lifecycle.v1" in channels
    assert "loom.coord" in channels
    assert "exec.lifecycle.v1" in channels
    assert "agent.message.v1" in channels
    assert "agent.session" in channels
    # The const-without-dot is rejected by looks_like_channel.
    assert "not-a-channel-without-dots" not in channels


def test_go_streambase_prefix_pattern(tmp_path: Path) -> None:
    """CRITICAL: when a Go file uses the `streamBase + "." + stream` runtime
    concatenation, the effective wire-channel is the prefixed form.

    This is the exact pattern in hearth-loom/internal/bus/publisher.go:57. The
    scanner MUST synthesise `<streamBase>.<const-value>` channels so the
    coverage check sees what actually crosses the bus.
    """
    fixture = tmp_path / "publisher.go"
    fixture.write_text(
        '''package bus

const BeadLifecycleStream = "bead.lifecycle.v1"
const ExecLifecycleStream = "exec.lifecycle.v1"

type Publisher struct {
    streamBase string
}

func NewPublisher(streamBase string) *Publisher {
    if streamBase == "" {
        streamBase = "bus"
    }
    return &Publisher{streamBase: streamBase}
}

func (p *Publisher) Publish(ctx context.Context, stream string, data any) {
    // This is the literal pattern that produces a prefixed channel name.
    key := p.streamBase + "." + stream
    _ = key
}
'''
    )
    out = run_scanner(fixture)
    channels = sorted({r["channel"] for r in out})

    # Both the suffix form (literal const) and the prefixed effective form
    # must appear — the scanner records both.
    assert "bead.lifecycle.v1" in channels
    assert "bus.bead.lifecycle.v1" in channels, (
        "scanner failed to synthesise prefixed channel from streamBase concat "
        f"pattern; got: {channels}"
    )
    assert "exec.lifecycle.v1" in channels
    assert "bus.exec.lifecycle.v1" in channels


def test_go_no_streambase_no_prefix(tmp_path: Path) -> None:
    """Negative control: without the streamBase concat pattern, the scanner
    must NOT synthesise prefixed channels."""
    fixture = tmp_path / "no_prefix.go"
    fixture.write_text(
        '''package bus
const SomeStream = "thing.foo.v1"
func emit(p *Publisher) {
    p.Publish(ctx, SomeStream, evt)
}
'''
    )
    out = run_scanner(fixture)
    channels = sorted({r["channel"] for r in out})
    assert "thing.foo.v1" in channels
    # No prefix should appear because the concat pattern is absent.
    assert "bus.thing.foo.v1" not in channels


# ─── Rust ────────────────────────────────────────────────────────────────────


def test_rust_publish_macro_and_const(tmp_path: Path) -> None:
    fixture = tmp_path / "lib.rs"
    fixture.write_text(
        '''
pub const CHANNEL_LIFECYCLE: &str = "loom.lifecycle.v1";
pub const STREAM_BEAD: &str = "bus.bead.lifecycle.v1";

fn main() {
    publish!("autobench.iteration.v1", payload);
}
'''
    )
    out = run_scanner(fixture)
    channels = sorted({r["channel"] for r in out})
    assert "loom.lifecycle.v1" in channels
    assert "bus.bead.lifecycle.v1" in channels
    assert "autobench.iteration.v1" in channels

    # Emit types tagged correctly.
    for r in out:
        if r["channel"] == "autobench.iteration.v1":
            assert r["emit_type"] == "call"
        if r["channel"] == "loom.lifecycle.v1":
            assert r["emit_type"] == "const"


# ─── Shell ───────────────────────────────────────────────────────────────────


def test_shell_nervous_publish(tmp_path: Path) -> None:
    fixture = tmp_path / "emit.sh"
    fixture.write_text(
        """#!/bin/bash
# This is a comment: nervous publish should-not-match-from-comments here
nervous publish bus.dashboard.v1 '{"foo":1}'
some_var=$(nervous publish autobench.budget.warning.v1 "$payload")
nervous publish single 'foo'        # rejected — no dot
"""
    )
    out = run_scanner(fixture)
    channels = sorted({r["channel"] for r in out})
    assert "bus.dashboard.v1" in channels
    assert "autobench.budget.warning.v1" in channels
    assert "should-not-match-from-comments" not in channels
    assert "single" not in channels


def test_shell_zellij_pipe(tmp_path: Path) -> None:
    fixture = tmp_path / "zellij.sh"
    fixture.write_text(
        """#!/bin/bash
zellij pipe -p nervous-bus -n bus.bead.lifecycle.v1 -- '{}'
zellij pipe -p nervous-bus -n loom.coord.v1 -- '{}'
"""
    )
    out = run_scanner(fixture)
    channels = sorted({r["channel"] for r in out})
    assert "bus.bead.lifecycle.v1" in channels
    assert "loom.coord.v1" in channels


# ─── Output format ───────────────────────────────────────────────────────────


def test_output_is_sorted_and_deduped(tmp_path: Path) -> None:
    fixture_a = tmp_path / "a.py"
    fixture_a.write_text('nbus.publish("zz.b.v1", {})\nnbus.publish("aa.x.v1", {})\n')
    fixture_b = tmp_path / "b.py"
    fixture_b.write_text('nbus.publish("aa.x.v1", {})\n')
    out = run_scanner(f"prod-x={fixture_a.parent}")
    # Records sorted by file, then line. Identical (producer,file,line,channel)
    # tuples should be deduped.
    seen_keys: set[tuple] = set()
    for r in out:
        k = (r["producer"], r["file"], r["line"], r["channel"])
        assert k not in seen_keys, f"duplicate record: {k}"
        seen_keys.add(k)
    # Producer label preserved.
    assert all(r["producer"] == "prod-x" for r in out)


def test_producer_label_from_path_prefix(tmp_path: Path) -> None:
    fixture = tmp_path / "x.py"
    fixture.write_text('nbus.publish("loom.coord", {})\n')
    out = run_scanner(f"my-producer={tmp_path}")
    assert out
    assert all(r["producer"] == "my-producer" for r in out)


def test_record_has_emit_type_field(tmp_path: Path) -> None:
    """Every record output by the scanner must carry an `emit_type` field
    with value 'const' or 'call'."""
    fixture = tmp_path / "mixed.py"
    fixture.write_text(
        '''
CHANNEL_FOO = "foo.bar.v1"
nbus.publish("baz.qux.v1", {})
'''
    )
    out = run_scanner(fixture)
    assert len(out) == 2
    for r in out:
        assert "emit_type" in r
        assert r["emit_type"] in {"const", "call"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-xvs"]))
