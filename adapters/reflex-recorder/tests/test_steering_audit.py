"""steering_audit: thresholds from fetched text, import chains, findings, crosswalk."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import steering_audit as sa

SOURCES = '''
[[source]]
id = "cc-memory"
url = "https://x/memory.md"

[[source]]
id = "cc-best-practices"
url = "https://x/bp.md"

[[threshold]]
id = "claude_md_target_lines"
source = "cc-memory"
pattern = 'target under (\\d+) lines per CLAUDE\\.md file'

[[threshold]]
id = "import_max_hops"
source = "cc-memory"
pattern = 'maximum depth of (\\w+) hops'

[[threshold]]
id = "memory_index_lines"
source = "cc-memory"
pattern = 'The first (\\d+) lines of `MEMORY\\.md`'
'''

BP = """# Best practices

## Avoid common failure patterns

* **The kitchen sink session.** You start with one task.
* **A brand new pattern.** Something.

## Develop your intuition

* **Not a pattern.** ignored
"""

CROSSWALK = '''
[pattern."the kitchen sink session"]
detectors = ["reread_same_file"]
'''


def _world(tmp_path: Path, memory_text: str) -> dict:
    g = tmp_path / "guidance"
    (g / "sources").mkdir(parents=True)
    (g / "sources" / "cc-memory.md").write_text(memory_text)
    (g / "sources" / "cc-best-practices.md").write_text(BP)
    home = tmp_path / "claude"
    (home / "memory-global").mkdir(parents=True)
    (home / "CLAUDE.md").write_text("top\n@GLOBAL.md\n`@NOT-IMPORTED.md`\n")
    (home / "GLOBAL.md").write_text("@memory-global/a.md\n")
    (home / "memory-global" / "a.md").write_text("NEVER do x 2026-01-01\nENFORCED by hook\n" * 3)
    (home / "NOT-IMPORTED.md").write_text("x\n" * 999)
    mem = home / "projects" / "-p" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- line\n" * 12)
    proj = tmp_path / "projects"
    (proj / "loaded").mkdir(parents=True)
    (proj / "loaded" / "CLAUDE.md").write_text("@AGENTS.md\n")
    (proj / "loaded" / "AGENTS.md").write_text("rule\n" * 30)
    (proj / "vendored").mkdir()
    (proj / "vendored" / "AGENTS.md").write_text("rule\n" * 30)
    tokens = tmp_path / "tokens.db"
    c = sqlite3.connect(tokens)
    c.execute("CREATE TABLE sessions (key TEXT, started TEXT)")
    c.execute("CREATE TABLE instructions (session_key TEXT, path TEXT, kind TEXT, chars INT)")
    c.execute("INSERT INTO sessions VALUES ('s', '2026-10-17T00:00:00')")
    wt = str(tmp_path).replace("/projects", "") + "/data2/worktrees/loaded/wt1/CLAUDE.md"
    c.execute("INSERT INTO instructions VALUES ('s', ?, 'Project', 40)", (wt,))
    c.commit()
    (tmp_path / "s.toml").write_text(SOURCES)
    (tmp_path / "cw.toml").write_text(CROSSWALK)
    return dict(gdir=g, claude_home=home, projects_root=proj, tokens_db=tokens,
                runs_db=tmp_path / "none.db", token_digest=tmp_path / "none.json",
                sources_toml=tmp_path / "s.toml", crosswalk_toml=tmp_path / "cw.toml",
                now=datetime(2026, 10, 18, tzinfo=timezone.utc))


MEMORY_DOC = ("target under 10 lines per CLAUDE.md file\nmaximum depth of four hops\n"
              "The first 5 lines of `MEMORY.md`, or the first 25KB\n")


def test_thresholds_read_from_source_and_words(tmp_path):
    rep = sa.build(**_world(tmp_path, MEMORY_DOC))
    th = rep["thresholds"]
    assert th["claude_md_target_lines"]["value"] == 10
    assert th["import_max_hops"]["value"] == 4
    assert th["claude_md_target_lines"]["line"] == 1


def test_import_chain_skips_code_spans_and_counts_metrics(tmp_path):
    rep = sa.build(**_world(tmp_path, MEMORY_DOC))
    chain = rep["global_chain"]
    names = [Path(f["path"]).name for f in chain["files"]]
    assert names == ["CLAUDE.md", "GLOBAL.md", "a.md"]
    a = chain["files"][-1]
    assert (a["hops"], a["emphasis_lines"], a["dated_lines"]) == (2, 3, 3)
    assert a["enforced_lines"] == [2, 4, 6]


def test_findings_only_from_fetched_limits_and_loaded_roots(tmp_path):
    rep = sa.build(**_world(tmp_path, MEMORY_DOC))
    rules = [(f["rule"], Path(f.get("path", f.get("source", ""))).parent.name) for f in rep["findings"]]
    assert ("claude_md_over_target", "loaded") in rules          # 31-line chain > 10, loaded via worktree
    assert not any(r == "claude_md_over_target" and p == "vendored" for r, p in rules)
    assert ("memory_index_truncated", "memory") in rules         # 12 lines > 5
    over = [f for f in rep["findings"] if f["rule"] == "claude_md_over_target"]
    assert all(f["source"] == "cc-memory" and f["source_line"] == 1 for f in over)


def test_unresolved_threshold_is_a_finding_not_a_default(tmp_path):
    rep = sa.build(**_world(tmp_path, "the wording moved\n"))
    unresolved = {f["threshold"] for f in rep["findings"] if f["rule"] == "threshold_unresolved"}
    assert unresolved == {"claude_md_target_lines", "import_max_hops", "memory_index_lines"}
    assert not any(f["rule"] == "claude_md_over_target" for f in rep["findings"])


def test_failure_crosswalk_and_unmapped_pattern(tmp_path):
    rep = sa.build(**_world(tmp_path, MEMORY_DOC))
    xw = {r["pattern"]: r for r in rep["failure_crosswalk"]}
    assert set(xw) == {"the kitchen sink session", "a brand new pattern"}
    assert xw["the kitchen sink session"]["mapped"] and not xw["a brand new pattern"]["mapped"]
    assert any(f["rule"] == "failure_pattern_unmapped" and f["pattern"] == "a brand new pattern"
               for f in rep["findings"])


def test_canonical_path_folds_worktrees():
    assert sa.canonical_path("/h/data2/worktrees/proj/wt/sub/CLAUDE.md") == "/h/projects/proj/sub/CLAUDE.md"
    assert sa.canonical_path("/h/projects/proj/CLAUDE.md") == "/h/projects/proj/CLAUDE.md"


def test_markdown_renders(tmp_path):
    rep = sa.build(**_world(tmp_path, MEMORY_DOC))
    md = sa.to_markdown(rep)
    assert "claude_md_over_target" in md and "Doc failure patterns" in md
    json.dumps(rep)  # serializable


def test_last_audit_is_the_gate_stamp_not_the_nightly(tmp_path):
    (tmp_path / "latest.json").write_text(json.dumps({"generated_at": "2026-10-18T05:30:00Z"}))
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "dry.json").write_text("{}")
    assert sa._last_audit(tmp_path) is None   # nightly digest and dry runs are not audits
    (tmp_path / "last_audit").write_text("2026-10-18T19:32:26Z\n")
    assert sa._last_audit(tmp_path) == "2026-10-18T19:32:26Z"


def test_changes_window_uses_last_audit(tmp_path):
    w = _world(tmp_path, MEMORY_DOC)
    (w["gdir"] / "changes.jsonl").write_text(
        json.dumps({"ts": "2026-10-01T00:00:00Z", "id": "cc-memory"}) + "\n"
        + json.dumps({"ts": "2026-10-17T00:00:00Z", "id": "cc-best-practices"}) + "\n")
    rep = sa.build(**w, last_audit="2026-09-30T00:00:00Z")
    assert [c["id"] for c in rep["guidance_changes"]] == ["cc-memory", "cc-best-practices"]
    rep = sa.build(**w)  # no gated run yet: the window is the fallback
    assert [c["id"] for c in rep["guidance_changes"]] == ["cc-best-practices"]
