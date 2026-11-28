"""Tests for adapters/forge against fake `agy` and `blender` executables on PATH.

The live smoke test runs real Blender: `pytest -m live` (CI runs `-m "not live"`).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge as f

ADAPTER_DIR = Path(__file__).resolve().parent
CLI = ADAPTER_DIR / "forge"

PNG_HELPER = r"""
import struct, zlib
def png(w=4, h=3):
    def chunk(t, b):
        c = struct.pack(">I", len(b)) + t + b
        return c + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x80\x80\x80\xff" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
"""

FAKE_AGY = "#!/usr/bin/env python3\n" + PNG_HELPER + r"""
import os, re, sys, time
mode = os.environ.get("FAKE_AGY_MODE", "ok")
prompt = sys.argv[sys.argv.index("-p") + 1]
name = re.search(r"save it as (\S+) in", prompt).group(1)
if mode == "timeout":
    time.sleep(30)
elif mode == "missing" and name == "ref_02.png":
    pass
elif mode == "bad":
    open(name, "wb").write(b"not a png at all")
elif mode == "truncated":
    open(name, "wb").write(png()[:-20])
else:
    open(name, "wb").write(png())
"""

FAKE_BLENDER = "#!/usr/bin/env python3\n" + PNG_HELPER + r"""
import json, os, sys, time
mode = os.environ.get("FAKE_BLENDER_MODE", "ok")
argv = sys.argv[1:]
script = os.path.basename(argv[argv.index("--python") + 1])
rest = argv[argv.index("--") + 1:]
opt = dict(zip([k.lstrip("-") for k in rest[::2]], rest[1::2]))
if script == "forge_render.py":
    if mode == "render_fail":
        sys.exit(1)
    open(os.path.join(opt["out"], "sheet.png"), "wb").write(png(70, 20))
    json.dump({"engine": "FAKE", "triangles": 12, "bounds_min": [-1, -1, -1], "bounds_max": [1, 1, 1],
               "extent": [2, 2, 2], "views": ["front"]}, open(os.path.join(opt["out"], "render_info.json"), "w"))
elif script == "forge_compose.py":
    open(opt["out"], "wb").write(png(90, 20))
else:
    if mode == "timeout":
        time.sleep(30)
    if mode == "crash":
        sys.stderr.write("Traceback: boom\n")
        sys.exit(1)
    if mode != "no_glb":
        open(os.path.join(os.environ["FORGE_OUT"], "model.glb"), "wb").write(b"glTF")
"""

FAKE_GATE = r"""
import json, sys
print(json.dumps({"ok": "--fail" not in open(sys.argv[3]).read(), "path": sys.argv[1]}))
sys.exit(1 if "--fail" in open(sys.argv[3]).read() else 0)
"""


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "agy", FAKE_AGY)
    _exe(bindir / "blender", FAKE_BLENDER)
    e = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FORGE_NETNS="off")
    e.pop("FORGE_MESH_GATE", None)
    return e


def forge(env, *args):
    r = subprocess.run([str(CLI), *map(str, args)], env=env, capture_output=True, text=True, timeout=60)
    return r.returncode, json.loads(r.stdout) if r.stdout.strip() else None, r.stderr


@pytest.fixture()
def prompt(tmp_path):
    p = tmp_path / "prompt.txt"
    p.write_text("A wooden crate, three-quarter view, plain background")
    return p


def test_ref_ok(env, prompt, tmp_path):
    out = tmp_path / "refs"
    rc, j, _ = forge(env, "ref", "--prompt-file", prompt, "--out", out, "--n", 2)
    assert rc == 0 and j["ok"]
    side = json.loads((out / "asset.json").read_text())
    assert side["ai_generated"] is True and side["player_facing"] is False
    assert side["model"] == "gemini-4-neon-medium" and "crate" in side["prompt"]
    assert set(side["files"]) == {"ref_01.png", "ref_02.png"}
    import hashlib
    assert side["files"]["ref_01.png"] == hashlib.sha256((out / "ref_01.png").read_bytes()).hexdigest()


def test_ref_missing_file(env, prompt, tmp_path):
    env["FAKE_AGY_MODE"] = "missing"
    rc, j, _ = forge(env, "ref", "--prompt-file", prompt, "--out", tmp_path / "r", "--n", 2)
    assert rc == 1 and not j["ok"]
    assert j["errors"][0]["file"] == "ref_02.png" and "missing" in j["errors"][0]["reason"]
    assert [f["file"] for f in j["files"]] == ["ref_01.png"]


@pytest.mark.parametrize("mode", ["bad", "truncated"])
def test_ref_bad_png(env, prompt, tmp_path, mode):
    env["FAKE_AGY_MODE"] = mode
    rc, j, _ = forge(env, "ref", "--prompt-file", prompt, "--out", tmp_path / "r")
    assert rc == 1 and j["errors"][0]["file"] == "ref_01.png"


def test_ref_timeout(env, prompt, tmp_path):
    env["FAKE_AGY_MODE"] = "timeout"
    rc, j, _ = forge(env, "ref", "--prompt-file", prompt, "--out", tmp_path / "r", "--timeout", 1)
    assert rc == 1 and "timed out" in j["errors"][0]["reason"]


def test_ref_usage_and_unreachable(env, prompt, tmp_path):
    rc, _, _ = forge(env, "ref", "--prompt-file", tmp_path / "nope", "--out", tmp_path / "r")
    assert rc == 2
    env["PATH"] = str(tmp_path / "empty")
    os.makedirs(env["PATH"])
    r = subprocess.run(["/usr/bin/python3", "-I", str(ADAPTER_DIR / "forge.py"), "ref", "--prompt-file",
                        str(prompt), "--out", str(tmp_path / "r")], env=env, capture_output=True, text=True)
    assert r.returncode == 3


def test_no_args_is_usage(env):
    r = subprocess.run([str(CLI)], env=env, capture_output=True, text=True)
    assert r.returncode == 2


@pytest.fixture()
def script(tmp_path):
    s = tmp_path / "model.py"
    s.write_text("# fake, the fake blender ignores the body\n")
    return s


def test_blend_ok(env, script, tmp_path):
    out = tmp_path / "asset"
    rc, j, _ = forge(env, "blend", "--script", script, "--out", out)
    assert rc == 0 and j["ok"]
    assert j["polycount"] == 12 and j["bounds"]["extent"] == [2, 2, 2]
    assert (out / "model.glb").is_file() and (out / "sheet.png").is_file()
    assert {"script_seconds", "render_seconds"} <= set(j["timings"])
    assert j["network_isolation"].startswith("none")


def test_blend_reports_isolation(env, script, tmp_path):
    env["FORGE_NETNS"] = "auto"
    rc, j, _ = forge(env, "blend", "--script", script, "--out", tmp_path / "a")
    assert rc == 0
    assert j["network_isolation"] == "unshare -rn" or "unavailable" in j["network_isolation"]


def test_blend_timeout(env, script, tmp_path):
    env["FAKE_BLENDER_MODE"] = "timeout"
    rc, j, _ = forge(env, "blend", "--script", script, "--out", tmp_path / "a", "--timeout", 1)
    assert rc == 1 and "timed out" in j["reason"]


def test_blend_no_glb(env, script, tmp_path):
    env["FAKE_BLENDER_MODE"] = "no_glb"
    rc, j, _ = forge(env, "blend", "--script", script, "--out", tmp_path / "a")
    assert rc == 1 and "model.glb" in j["reason"]


def test_blend_crash_and_render_fail(env, script, tmp_path):
    env["FAKE_BLENDER_MODE"] = "crash"
    rc, j, _ = forge(env, "blend", "--script", script, "--out", tmp_path / "a")
    assert rc == 1 and "boom" in j["stderr_tail"]
    env["FAKE_BLENDER_MODE"] = "render_fail"
    rc, j, _ = forge(env, "blend", "--script", script, "--out", tmp_path / "b")
    assert rc == 1 and "render failed" in j["reason"]


def test_blend_stale_glb_not_reused(env, script, tmp_path):
    out = tmp_path / "a"
    out.mkdir()
    (out / "model.glb").write_bytes(b"old")
    env["FAKE_BLENDER_MODE"] = "no_glb"
    rc, j, _ = forge(env, "blend", "--script", script, "--out", out)
    assert rc == 1


def test_gate_skipped_without_env(env, tmp_path):
    rc, j, _ = forge(env, "gate", tmp_path / "x.glb", "--profile", tmp_path / "p.toml")
    assert rc == 0 and j["status"] == "skipped"


def test_gate_passthrough(env, tmp_path):
    gate = tmp_path / "gate.py"
    gate.write_text(FAKE_GATE)
    env["FORGE_MESH_GATE"] = str(gate)
    glb = tmp_path / "x.glb"
    glb.write_bytes(b"glTF")
    prof = tmp_path / "p.toml"
    prof.write_text("[mesh]\n")
    rc, j, _ = forge(env, "gate", glb, "--profile", prof)
    assert rc == 0 and j["ok"] is True
    prof.write_text("--fail\n")
    rc, j, _ = forge(env, "gate", glb, "--profile", prof)
    assert rc == 1 and j["ok"] is False


def test_compare(env, prompt, tmp_path):
    refs = tmp_path / "refs"
    forge(env, "ref", "--prompt-file", prompt, "--out", refs, "--n", 2)
    sheet = tmp_path / "sheet.png"
    ns: dict = {}
    exec(PNG_HELPER, ns)
    sheet.write_bytes(ns["png"](70, 20))
    out = tmp_path / "cmp.png"
    rc, j, _ = forge(env, "compare", sheet, refs, "--out", out)
    assert rc == 0 and j["compare"] == str(out) and len(j["refs"]) == 2
    assert f.check_png(out) == (90, 20)


def test_compare_bad_sheet(env, prompt, tmp_path):
    refs = tmp_path / "refs"
    forge(env, "ref", "--prompt-file", prompt, "--out", refs)
    bad = tmp_path / "sheet.png"
    bad.write_bytes(b"junk")
    rc, j, _ = forge(env, "compare", bad, refs)
    assert rc == 1


CUBE = """
import bpy, os
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2)
out = os.environ["FORGE_OUT"]
bpy.ops.export_scene.gltf(filepath=os.path.join(out, "model.glb"), export_format="GLB")
"""


@pytest.mark.live
@pytest.mark.skipif(shutil.which("blender") is None, reason="blender not installed")
def test_live_cube(tmp_path):
    s = tmp_path / "cube.py"
    s.write_text(CUBE)
    out = tmp_path / "cube"
    e = dict(os.environ)
    rc, j, err = forge(e, "blend", "--script", s, "--out", out, "--timeout", 240)
    assert rc == 0, (j, err)
    assert j["polycount"] == 12
    assert j["bounds"]["extent"] == pytest.approx([2, 2, 2], abs=1e-3)
    assert f.check_png(out / "sheet.png") == (384 * 7, 384 * 2)
    refs = tmp_path / "refs"
    refs.mkdir()
    shutil.copy(out / "views" / "color_front.png", refs / "ref_01.png")
    rc, j, err = forge(e, "compare", out / "sheet.png", refs)
    assert rc == 0, (j, err)
