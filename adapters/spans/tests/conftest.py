"""Fixtures for the spans tests: a fake `nervous`, a temp transcript root and project roots."""
import importlib
import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
spans = importlib.import_module("spans")

# A fake `nervous`: publish-batch appends its stdin lines to TMP/sent and prints the summary line.
FAKE = r"""#!/bin/sh
[ -f "TMP/fail" ] && exit 7
[ "$1" = publish-batch ] && [ "$2" = agent.tool.span.v1 ] || exit 9
echo call >> "TMP/calls"
body=$(cat)
printf '%s\n' "$body" >> "TMP/sent"
printf '{"published":%s,"rejected":[]}\n' "$(printf '%s\n' "$body" | grep -c .)"
"""
FAKE_FAIL = 'n=$(cat "TMP/n" 2>/dev/null || echo 0); echo $((n+1)) > "TMP/n"; [ "$n" -ge 1 ] && exit 7\nbody=$(cat)'


@pytest.fixture
def world(tmp_path, monkeypatch):
    projects = tmp_path / "work" / "projects"
    for p in ("alpha", "beta"):
        (projects / p).mkdir(parents=True)
    monkeypatch.setenv("NBUS_PROJECT_ROOTS", str(projects))
    monkeypatch.delenv("NBUS_PROJECT_ALIASES", raising=False)
    root = tmp_path / "claude" / "projects"
    (root / "-alpha").mkdir(parents=True)
    fake = tmp_path / "nervous"
    fake.write_text(FAKE.replace("TMP", str(tmp_path)))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    class W:
        pass
    w = W()
    w.tmp, w.root, w.fake, w.alpha, w.beta = tmp_path, root, fake, str(projects / "alpha"), str(projects / "beta")
    w.cursor = tmp_path / "cursor.json"
    w.session = root / "-alpha" / "s1.jsonl"
    w.sent = lambda: [json.loads(x) for x in (tmp_path / "sent").read_text().splitlines()] if (tmp_path / "sent").exists() else []
    w.write = lambda *lines, path=None, mode="w": (path or w.session).open(mode).write("\n".join(lines) + "\n")
    w.scan = lambda *extra: spans.main(["scan", "--root", str(root), "--cursor", str(w.cursor), "--nervous", str(fake), *extra])
    return w
