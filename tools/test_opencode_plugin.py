"""agent-hooks/opencode/nervous-bus.js driven by node against stub argshape and nervous binaries."""
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup as S  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "agent-hooks" / "opencode" / "nervous-bus.js"
NODE = shutil.which("node")

def _node_major() -> int:
    if not NODE:
        return 0
    out = subprocess.run([NODE, "--version"], capture_output=True, text=True).stdout.strip()
    return int(out.lstrip("v").split(".")[0] or 0)


needs_node = pytest.mark.skipif(_node_major() < 16 or sys.platform == "win32",
                                reason="needs node >= 16 (top-level await) and POSIX stubs")

DRIVER = r"""
const mod = await import(process.argv[2] + "?t=" + Date.now());
const useDollar = process.argv[3] === "dollar";
const calls = [];
const dollar = (strings, ...vals) => {
  calls.push(vals.map(String));
  const p = { quiet: () => p, nothrow: async () => ({ exitCode: 0 }) };
  return p;
};
const hooks = await mod.NervousBus({ $: useDollar ? dollar : undefined, directory: "/work/proj" });
await hooks.event({ event: { type: "session.created", properties: { info: { id: "ses_1", directory: "/work/proj", title: "t" } } } });
await hooks.event({ event: { type: "session.idle", properties: { sessionID: "ses_1" } } });
await hooks.event({ event: { type: "message.updated", properties: {} } });
const out = { args: { command: "ls --badflag" } };
await hooks["tool.execute.before"]({ tool: "bash", sessionID: "ses_1", callID: "c1" }, out);
await hooks["tool.execute.before"]({ tool: "read", sessionID: "ses_1", callID: "c2" }, { args: { filePath: "x" } });
await hooks["tool.execute.after"]({ tool: "bash", sessionID: "ses_1", callID: "c1" }, { output: "boom", metadata: { exit: 2 } });
await hooks["tool.execute.after"]({ tool: "bash", sessionID: "ses_1", callID: "c3", args: { command: "true" } }, { output: "", metadata: { exit: 0 } });
console.log(JSON.stringify({ command: out.args.command, calls }));
"""


def _stub(path: Path, body: str):
    path.write_text("#!/usr/bin/env python3\nimport json, os, sys\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def stubs(tmp_path):
    log = tmp_path / "log.jsonl"
    _stub(tmp_path / "argshape", f"""
ev = json.load(sys.stdin)
open({str(log)!r}, "a").write(json.dumps({{"kind": sys.argv[2], "ev": ev}}) + "\\n")
if sys.argv[2] == "pre":
    print(json.dumps({{"hookSpecificOutput": {{"hookEventName": "PreToolUse",
        "updatedInput": {{"command": ev["tool_input"]["command"].replace("--badflag", "--good")}}}}}}))
""")
    _stub(tmp_path / "nervous", f"""
open({str(log)!r}, "a").write(json.dumps({{"kind": "publish", "argv": sys.argv[1:]}}) + "\\n")
""")
    (tmp_path / "driver.mjs").write_text(DRIVER)
    shutil.copyfile(PLUGIN, tmp_path / "plugin.mjs")
    return tmp_path, log


def _run(stubs, mode):
    d, log = stubs
    env = {"PATH": f"{d}:/usr/bin:/bin", "ARGSHAPE_BIN": str(d / "argshape"), "NERVOUS_BIN": str(d / "nervous")}
    p = subprocess.run([NODE, str(d / "driver.mjs"), (d / "plugin.mjs").as_uri(), mode], capture_output=True, text=True,
                       env=env, timeout=30)
    assert p.returncode == 0, p.stderr
    rows = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return json.loads(p.stdout), rows


@needs_node
def test_publishes_session_events_through_dollar(stubs):
    out, rows = _run(stubs, "dollar")
    pubs = out["calls"]
    assert len(pubs) == 2
    assert all(c[1] == "agent.opencode.session" for c in pubs)
    first = json.loads(pubs[0][2])
    assert first["event"] == "started" and first["session_id"] == "ses_1" and first["title"] == "t"
    assert json.loads(pubs[1][2])["event"] == "idle"
    assert not [r for r in rows if r["kind"] == "publish"]


@needs_node
def test_publish_falls_back_without_dollar(stubs):
    _, rows = _run(stubs, "nodollar")
    pubs = [r for r in rows if r["kind"] == "publish"]
    assert len(pubs) == 2 and pubs[0]["argv"][:2] == ["publish", "agent.opencode.session"]


@needs_node
def test_bash_hooks_rewrite_and_settle(stubs):
    out, rows = _run(stubs, "dollar")
    assert out["command"] == "ls --good"
    kinds = [r["kind"] for r in rows]
    assert kinds == ["pre", "fail", "post"]
    assert rows[1]["ev"]["tool_input"]["command"] == "ls --good" and rows[1]["ev"]["error"] == "boom"
    assert rows[2]["ev"]["tool_input"]["command"] == "true"
    assert rows[0]["ev"]["session_id"] == "ses_1" and rows[0]["ev"]["tool_name"] == "Bash"


@needs_node
def test_missing_binaries_never_throw(tmp_path):
    (tmp_path / "driver.mjs").write_text(DRIVER)
    shutil.copyfile(PLUGIN, tmp_path / "plugin.mjs")
    env = {"PATH": "/usr/bin:/bin", "ARGSHAPE_BIN": "/nonexistent/a", "NERVOUS_BIN": "/nonexistent/n"}
    p = subprocess.run([NODE, str(tmp_path / "driver.mjs"), (tmp_path / "plugin.mjs").as_uri(), "nodollar"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["command"] == "ls --badflag"


def test_payloads_match_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO / "schemas" / "agent.opencode.session.v1.json").read_text())
    jsonschema.validate({"event": "started", "session_id": "ses_1", "cwd": "/w", "title": "t",
                         "parent_session_id": "ses_0", "ts": "2026-11-26T00:00:00.000Z"}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"event": "bogus", "session_id": "s", "ts": "x"}, schema)


def test_setup_installs_and_removes_plugin(tmp_path):
    dest = tmp_path / "plugins"
    assert S.install_opencode_plugin(dest, dry_run=True)["action"] == "would install"
    assert not dest.exists()
    assert S.install_opencode_plugin(dest)["action"] == "installed"
    assert (dest / "nervous-bus.js").read_bytes() == PLUGIN.read_bytes()
    assert S.install_opencode_plugin(dest)["action"] == "unchanged"
    (dest / "nervous-bus.js").write_text("old")
    assert S.install_opencode_plugin(dest)["action"] == "updated"
    assert S.install_opencode_plugin(dest, remove=True)["action"] == "removed"
    assert S.install_opencode_plugin(dest, remove=True)["action"] == "absent"
