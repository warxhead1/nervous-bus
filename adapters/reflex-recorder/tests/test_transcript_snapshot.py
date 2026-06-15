"""test_transcript_snapshot.py — unit tests for the transcript snapshotter.

All fixtures are synthetic; we never touch a real ~/.claude/projects tree.
Tests cover the six behaviors the spec requires:

  1. Fresh copy: single .jsonl -> identical dst, manifest recorded, files_new==1.
  2. Incremental append: appended line lands in dst, files_appended==1.
  3. No-change run: stats reflect zero work.
  4. Truncation/rotation: shorter (or replaced) src -> re-copy, files_recopied==1.
  5. Durability: deleting src leaves dst intact.
  6. Worktree-first ordering: worktree dirs are processed before normal dirs.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import transcript_snapshot as ts  # noqa: E402


def _record(uuid: str, content_text: str = "hi") -> str:
    """Build a synthetic Claude Code transcript line (one JSON object per line)."""
    return json.dumps({
        "uuid": uuid,
        "type": "assistant",
        "agentName": "Sculptor",
        "cwd": "/x/y",
        "sessionId": "s1",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "name": "Edit",
                "input": {"file_path": "/x/y/a.slang", "new_string": content_text},
            }],
        },
    })


def _write_jsonl(path: Path, n_lines: int) -> int:
    """Write n_lines of synthetic records to ``path`` (overwriting). Returns bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(_record(f"u{i}", f"line-{i}") for i in range(n_lines)) + "\n"
    path.write_text(body)
    return len(body.encode("utf-8"))


class TestFreshCopy(unittest.TestCase):
    """Test 1: cold start mirrors one src file into dst, manifest recorded."""

    def test_fresh_copy(self):
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as dst_root:
            src = Path(src_root)
            dst = Path(dst_root)
            # munged-dir pattern: '-x-y' is fine, but we want a realistic
            # layout; use a name that doesn't contain "worktrees".
            d = src / "-home-eric-projects-myapp"
            d.mkdir(parents=True)
            file_size = _write_jsonl(d / "sess-1.jsonl", 2)

            stats = ts.sync_once(src_root=src_root, dst_root=dst_root)

            self.assertEqual(stats["files_seen"], 1)
            self.assertEqual(stats["files_new"], 1)
            self.assertEqual(stats["files_appended"], 0)
            self.assertEqual(stats["files_recopied"], 0)
            self.assertEqual(stats["bytes_copied"], file_size)
            self.assertEqual(stats["errors"], [])

            # Byte-for-byte equality between src and dst.
            src_bytes = (d / "sess-1.jsonl").read_bytes()
            dst_bytes = (dst / "-home-eric-projects-myapp" / "sess-1.jsonl").read_bytes()
            self.assertEqual(dst_bytes, src_bytes)

            # Manifest: inode + size recorded for the one mirrored file.
            manifest = json.loads((dst / ".manifest.json").read_text())
            self.assertIn("-home-eric-projects-myapp/sess-1.jsonl", manifest)
            entry = manifest["-home-eric-projects-myapp/sess-1.jsonl"]
            self.assertEqual(entry["size"], file_size)
            self.assertIsInstance(entry["inode"], int)


class TestIncrementalAppend(unittest.TestCase):
    """Test 2: appending a line to src lands that exact line in dst."""

    def test_incremental_append(self):
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as dst_root:
            d = Path(src_root) / "-home-eric-projects-myapp"
            d.mkdir(parents=True)
            f = d / "sess-1.jsonl"
            _write_jsonl(f, 2)

            ts.sync_once(src_root=src_root, dst_root=dst_root)
            dst_path = Path(dst_root) / "-home-eric-projects-myapp" / "sess-1.jsonl"
            self.assertEqual(dst_path.read_bytes(), f.read_bytes())

            # Append a 3rd line to src. The file grows; inode is preserved
            # (we open the same path and only extend it).
            new_line = _record("u2", "line-2") + "\n"
            with open(f, "ab") as h:
                h.write(new_line.encode("utf-8"))
            self.assertEqual(f.read_bytes().count(b"\n"), 3)

            stats = ts.sync_once(src_root=src_root, dst_root=dst_root)

            self.assertEqual(stats["files_seen"], 1)
            self.assertEqual(stats["files_new"], 0)
            self.assertEqual(stats["files_appended"], 1)
            self.assertEqual(stats["files_recopied"], 0)
            self.assertEqual(stats["bytes_copied"], len(new_line.encode("utf-8")))

            # src == dst byte-for-byte.
            self.assertEqual(dst_path.read_bytes(), f.read_bytes())


class TestNoChange(unittest.TestCase):
    """Test 3: a second run with no src change is a no-op."""

    def test_no_change(self):
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as dst_root:
            d = Path(src_root) / "-home-eric-projects-myapp"
            d.mkdir(parents=True)
            _write_jsonl(d / "sess-1.jsonl", 3)

            ts.sync_once(src_root=src_root, dst_root=dst_root)
            stats = ts.sync_once(src_root=src_root, dst_root=dst_root)

            self.assertEqual(stats["files_seen"], 1)
            self.assertEqual(stats["files_new"], 0)
            self.assertEqual(stats["files_appended"], 0)
            self.assertEqual(stats["files_recopied"], 0)
            self.assertEqual(stats["bytes_copied"], 0)
            self.assertEqual(stats["errors"], [])


class TestTruncationRotation(unittest.TestCase):
    """Test 4: in-place shrink AND identity change both trigger a re-copy."""

    def test_inplace_shrink(self):
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as dst_root:
            d = Path(src_root) / "-home-eric-projects-myapp"
            d.mkdir(parents=True)
            f = d / "sess-1.jsonl"
            _write_jsonl(f, 5)

            ts.sync_once(src_root=src_root, dst_root=dst_root)
            dst_path = Path(dst_root) / "-home-eric-projects-myapp" / "sess-1.jsonl"

            # Truncate in place — same path, same inode, smaller size.
            f.write_text(_record("u-rotated", "rotated") + "\n")
            self.assertLess(f.stat().st_size, dst_path.stat().st_size)

            stats = ts.sync_once(src_root=src_root, dst_root=dst_root)

            self.assertEqual(stats["files_recopied"], 1)
            self.assertEqual(stats["files_new"], 0)
            self.assertEqual(stats["files_appended"], 0)
            self.assertEqual(dst_path.read_bytes(), f.read_bytes())

            # Manifest entry updated to the new size + (same) inode.
            manifest = json.loads((Path(dst_root) / ".manifest.json").read_text())
            entry = manifest["-home-eric-projects-myapp/sess-1.jsonl"]
            self.assertEqual(entry["size"], f.stat().st_size)

    def test_inode_replaced(self):
        """A unlink+rewrite changes the inode; that should also re-copy."""
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as dst_root:
            d = Path(src_root) / "-home-eric-projects-myapp"
            d.mkdir(parents=True)
            f = d / "sess-1.jsonl"
            _write_jsonl(f, 3)
            old_inode = f.stat().st_ino

            ts.sync_once(src_root=src_root, dst_root=dst_root)

            # Replace the file: unlink + create. New inode guaranteed on POSIX.
            os.unlink(f)
            _write_jsonl(f, 4)
            new_inode = f.stat().st_ino
            self.assertNotEqual(old_inode, new_inode)

            stats = ts.sync_once(src_root=src_root, dst_root=dst_root)
            self.assertEqual(stats["files_recopied"], 1)
            dst_path = Path(dst_root) / "-home-eric-projects-myapp" / "sess-1.jsonl"
            self.assertEqual(dst_path.read_bytes(), f.read_bytes())


class TestDurability(unittest.TestCase):
    """Test 5: removing src leaves dst intact."""

    def test_src_removed_dst_intact(self):
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as dst_root:
            d = Path(src_root) / "-home-eric-projects-myapp"
            d.mkdir(parents=True)
            f = d / "sess-1.jsonl"
            body = _write_jsonl(f, 4)

            ts.sync_once(src_root=src_root, dst_root=dst_root)
            dst_path = Path(dst_root) / "-home-eric-projects-myapp" / "sess-1.jsonl"
            dst_bytes_before = dst_path.read_bytes()
            self.assertEqual(len(dst_bytes_before), body)

            # Reap the worktree: delete the whole src dir.
            import shutil as _sh
            _sh.rmtree(d)
            self.assertFalse(d.exists())

            # Second pass: src is gone, but dst must survive.
            stats = ts.sync_once(src_root=src_root, dst_root=dst_root)
            self.assertEqual(stats["files_seen"], 0)
            self.assertEqual(stats["errors"], [])

            # Dst file untouched.
            self.assertTrue(dst_path.exists())
            self.assertEqual(dst_path.read_bytes(), dst_bytes_before)
            self.assertEqual(len(dst_path.read_bytes()), body)

            # Manifest preserved.
            manifest = json.loads((Path(dst_root) / ".manifest.json").read_text())
            self.assertIn("-home-eric-projects-myapp/sess-1.jsonl", manifest)


class TestWorktreeFirstOrdering(unittest.TestCase):
    """Test 6: worktree dirs are processed before normal dirs."""

    def test_worktree_processed_first(self):
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as dst_root:
            src = Path(src_root)
            # Two dirs: one normal, one worktree. The worktree must come first.
            (src / "-home-eric-projects-myapp").mkdir(parents=True)
            (src / "-home-eric-projects-myapp--claude-worktrees-agent-abc").mkdir(parents=True)
            (src / "-home-eric-projects-myapp" / "normal-session.jsonl").write_text(
                _record("n1", "n") + "\n"
            )
            (src / "-home-eric-projects-myapp--claude-worktrees-agent-abc" / "wt-session.jsonl").write_text(
                _record("w1", "w") + "\n"
            )

            stats = ts.sync_once(src_root=src_root, dst_root=dst_root)

            # The spec: process_order is the per-file attempt order. The
            # worktree file must appear before the normal one.
            order = stats["process_order"]
            self.assertEqual(len(order), 2)
            self.assertIn("worktrees", order[0])
            self.assertNotIn("worktrees", order[1])

            # Both files mirrored successfully.
            self.assertEqual(stats["files_seen"], 2)
            self.assertEqual(stats["files_new"], 2)
            self.assertEqual(stats["files_recopied"], 0)

            # Dst layout preserves the relative structure.
            self.assertTrue(
                (Path(dst_root) / "-home-eric-projects-myapp--claude-worktrees-agent-abc" / "wt-session.jsonl").exists()
            )
            self.assertTrue(
                (Path(dst_root) / "-home-eric-projects-myapp" / "normal-session.jsonl").exists()
            )


class TestStatsCLI(unittest.TestCase):
    """Smoke-test the print_stats() helper used by ``--stats``."""

    def test_stats_shape(self):
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as dst_root:
            d = Path(src_root) / "-x-y"
            d.mkdir(parents=True)
            n = _write_jsonl(d / "s.jsonl", 2)
            ts.sync_once(src_root=src_root, dst_root=dst_root)

            out = ts.print_stats(dst_root=dst_root)
            self.assertEqual(out["files"], 1)
            self.assertEqual(out["bytes"], n)


if __name__ == "__main__":
    unittest.main()
