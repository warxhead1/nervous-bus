"""Focused lifecycle-status rendering tests for gen_channels_md.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path


GENERATOR = Path(__file__).with_name("gen_channels_md.py")
SPEC = importlib.util.spec_from_file_location("gen_channels_md", GENERATOR)
assert SPEC is not None and SPEC.loader is not None
gen_channels_md = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gen_channels_md)


def test_active_status_is_not_rendered_as_silent() -> None:
    assert gen_channels_md.status_annotation("active-producer-and-consumer") == (
        "🟢 **active-producer-and-consumer**"
    )


def test_inactive_statuses_keep_the_existing_silent_marker() -> None:
    assert gen_channels_md.status_annotation("unconsumed") == "🔇 **unconsumed**"
    assert gen_channels_md.status_annotation("planned") == "🔇 **planned**"
