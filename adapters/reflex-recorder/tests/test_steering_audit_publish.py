"""steering_audit_publish: a proposal files only when every citation checks out."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "automation"))
import steering_audit_publish as pub  # noqa: E402

QUOTE = "Bloated CLAUDE.md files cause Claude to ignore your actual instructions!"


@pytest.fixture
def world(tmp_path):
    g = tmp_path / "guidance"
    (g / "sources").mkdir(parents=True)
    (g / "sources" / "cc-best-practices.md").write_text(f"Keep it concise.\n{QUOTE}\n")
    target = tmp_path / "steer" / "CLAUDE.md"
    target.parent.mkdir()
    target.write_text("line one\nNEVER do the thing, 2026-01-01 incident\nline three\n")
    return g, target, (tmp_path / "steer",)


def _prop(tgt: Path, **over) -> dict:
    p = {"title": "Move incident narrative out of CLAUDE.md", "rung": "inform", "action": "move",
         "target": {"path": str(tgt), "line_start": 2, "line_end": 2,
                    "excerpt": "NEVER do the thing,  2026-01-01"},
         "guidance": [{"source": "cc-best-practices", "quote": QUOTE.replace(" ", "  ")}],
         "rationale": "loaded 177x/week", "edit": "- line 2", "verify": "lines drop"}
    p.update(over)
    return p


def test_grounded_proposal_passes(world):
    g, target, roots = world
    assert pub.validate(_prop(target), g, [], roots)["lines"] == 3


@pytest.mark.parametrize("over,msg", [
    ({"rung": "vibes"}, "rung"),
    ({"action": "delete-everything"}, "action"),
    ({"guidance": []}, "no guidance"),
    ({"guidance": [{"source": "cc-best-practices", "quote": "the docs say to keep files under 50 lines"}]}, "not found verbatim"),
    ({"guidance": [{"source": "made-up", "quote": QUOTE}]}, "unknown guidance source"),
    ({"edit": ""}, "edit missing"),
    ({"title": "x" * 91}, "title"),
])
def test_rejections(world, over, msg):
    g, target, roots = world
    with pytest.raises(pub.Rejected, match=msg):
        pub.validate(_prop(target, **over), g, [], roots)


def test_excerpt_must_be_in_cited_lines(world):
    g, target, roots = world
    bad = _prop(target, target={"path": str(target), "line_start": 1, "line_end": 1,
                                "excerpt": "NEVER do the thing"})
    with pytest.raises(pub.Rejected, match="excerpt not found"):
        pub.validate(bad, g, [], roots)
    oob = _prop(target, target={"path": str(target), "line_start": 2, "line_end": 9, "excerpt": "x"})
    with pytest.raises(pub.Rejected, match="out of range"):
        pub.validate(oob, g, [], roots)


def test_target_outside_allowed_roots(world, tmp_path):
    g, target, _ = world
    with pytest.raises(pub.Rejected, match="outside"):
        pub.validate(_prop(target), g, [], (tmp_path / "elsewhere",))


def test_duplicate_title_and_target_cooldown(world):
    g, target, roots = world
    with pytest.raises(pub.Rejected, match="duplicate"):
        pub.validate(_prop(target), g, [{"title": "move incident narrative out of claude.md"}], roots)
    prior = [{"title": "other", "target_path": str(target), "filed": "2026-10-15"}]
    with pytest.raises(pub.Rejected, match="cooldown"):
        pub.validate(_prop(target), g, prior, roots, today=date(2026, 10, 18))
    assert pub.validate(_prop(target), g, prior, roots, today=date(2026, 11, 1))


def test_main_stamps_audit_clock_only_when_not_dry_run(world, tmp_path, monkeypatch):
    import json as _json
    g, target, roots = world
    monkeypatch.setattr(pub, "ALLOWED_ROOTS", roots)
    props = tmp_path / "p.json"
    props.write_text(_json.dumps({"run": "t", "proposals": [_prop(target, title="x" * 91)]}))
    ddir = tmp_path / "digest"
    argv = ["pub", "--proposals", str(props), "--digest-dir", str(ddir), "--guidance-dir", str(g), "--no-gh"]
    monkeypatch.setattr(sys, "argv", argv + ["--dry-run"])
    assert pub.main() == 2
    assert not (ddir / "last_audit").exists()
    monkeypatch.setattr(sys, "argv", argv)   # all rejected: no bd/gh call, but the audit happened
    assert pub.main() == 2
    assert (ddir / "last_audit").read_text().strip()
