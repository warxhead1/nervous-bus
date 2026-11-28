#!/usr/bin/env python3
"""forge: reference images (agy) and headless Blender asset builds, with gates.

JSON on stdout, diagnostics on stderr.
Exit codes: 0 ok, 1 negative result, 2 usage, 3 backend unreachable (agy, blender, mesh_gate deps).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

EXIT_OK, EXIT_NEGATIVE, EXIT_USAGE, EXIT_UNREACHABLE = 0, 1, 2, 3
HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = "gemini-4-neon-medium"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# bytes per pixel by (color type, bit depth)
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class Fail(Exception):
    def __init__(self, msg: str, code: int = EXIT_NEGATIVE, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


def emit(obj: dict, rc: int) -> int:
    print(json.dumps(obj, indent=2, sort_keys=True))
    return rc


def check_png(path: Path) -> tuple[int, int]:
    """Return (width, height) of a PNG whose pixel data decodes to the declared size."""
    if not path.is_file():
        raise Fail(f"{path.name}: file missing")
    data = path.read_bytes()
    if not data.startswith(PNG_MAGIC):
        raise Fail(f"{path.name}: not a PNG (bad magic bytes)")
    pos, ihdr, idat, seen_end = 8, None, [], False
    while pos + 8 <= len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if len(body) < n:
            raise Fail(f"{path.name}: truncated PNG chunk")
        if typ == b"IHDR":
            ihdr = body
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            seen_end = True
            break
        pos += 12 + n
    if ihdr is None or len(ihdr) != 13 or not idat or not seen_end:
        raise Fail(f"{path.name}: incomplete PNG (missing IHDR, IDAT or IEND)")
    w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", ihdr)
    if w == 0 or h == 0 or ctype not in _CHANNELS:
        raise Fail(f"{path.name}: invalid PNG header")
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as e:
        raise Fail(f"{path.name}: PNG pixel data does not decompress ({e})")
    if interlace == 0:
        stride = (w * _CHANNELS[ctype] * depth + 7) // 8
        if len(raw) != h * (stride + 1):
            raise Fail(f"{path.name}: decoded size {len(raw)} does not match {w}x{h}")
    elif len(raw) < h:
        raise Fail(f"{path.name}: decoded size too small for {w}x{h}")
    return w, h


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_tool(cmd: list[str], timeout: float, cwd: Path | None = None, env: dict | None = None):
    """Run cmd in its own process group; kill the whole group on timeout.

    Returns (returncode, stdout, stderr, timed_out, seconds)."""
    t0 = time.time()
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        timed_out = True
    return p.returncode, out or "", err or "", timed_out, round(time.time() - t0, 2)


def tail(s: str, n: int = 600) -> str:
    return s[-n:]


# ---------------------------------------------------------------- ref

def cmd_ref(a) -> int:
    pf, out = Path(a.prompt_file).resolve(), Path(a.out).resolve()
    if not pf.is_file():
        raise Fail(f"prompt file not found: {pf}", EXIT_USAGE)
    if a.n < 1:
        raise Fail("--n must be at least 1", EXIT_USAGE)
    prompt = pf.read_text(encoding="utf-8").strip()
    if not prompt:
        raise Fail("prompt file is empty", EXIT_USAGE)
    agy = shutil.which("agy")
    if not agy:
        raise Fail("agy not found on PATH", EXIT_UNREACHABLE)
    out.mkdir(parents=True, exist_ok=True)

    def one(i: int):
        name = f"ref_{i:02d}.png"
        full = (f"{prompt}\n\nGenerate one image and save it as {name} in the current directory. "
                f"Do not create any other file.")
        rc, so, se, to, secs = run_tool([agy, "-p", full, "--model", a.model], a.timeout, cwd=out)
        return name, rc, so, se, to, secs

    with ThreadPoolExecutor(max_workers=a.n) as ex:
        results = list(ex.map(one, range(1, a.n + 1)))

    files, errors = [], []
    for name, rc, so, se, to, secs in results:
        path = out / name
        try:
            if to:
                raise Fail(f"{name}: agy timed out after {a.timeout}s")
            w, h = check_png(path)
            files.append({"file": name, "sha256": sha256(path), "width": w, "height": h, "seconds": secs})
        except Fail as e:
            errors.append({"file": name, "reason": str(e), "agy_exit": rc, "stderr_tail": tail(se)})
    sidecar = {
        "tool": "agy",
        "model": a.model,
        "prompt": prompt,
        "ai_generated": True,
        "player_facing": False,
        "files": {f["file"]: f["sha256"] for f in files},
    }
    (out / "asset.json").write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    res = {"ok": not errors, "out": str(out), "asset_json": str(out / "asset.json"),
           "files": files, "errors": errors}
    return emit(res, EXIT_OK if not errors else EXIT_NEGATIVE)


# ---------------------------------------------------------------- blend

def gpu_probe() -> None:
    """Fail fast when the GPU driver does not answer.

    A wedged NVIDIA driver leaves every process that opens /dev/nvidia* in
    uninterruptible sleep, and blender opens it even for CPU renders. The probe
    opens the control node in a disposable child. A child that does not finish
    within FORGE_GPU_PROBE_TIMEOUT seconds is abandoned (it may be unkillable)
    and never waited on again. FORGE_GPU_DEVICE names the node (a missing node
    skips the probe); FORGE_GPU_PROBE_CMD replaces the child argv (for tests).
    """
    if not Path(os.environ.get("FORGE_GPU_DEVICE", "/dev/nvidiactl")).exists():
        return
    override = os.environ.get("FORGE_GPU_PROBE_CMD")
    dev = os.environ.get("FORGE_GPU_DEVICE", "/dev/nvidiactl")
    try:
        argv = shlex.split(override) if override else [
            sys.executable, "-I", "-c", "import os,sys; os.close(os.open(sys.argv[1], os.O_RDWR))", dev]
    except ValueError as e:
        raise Fail(f"FORGE_GPU_PROBE_CMD is not parseable: {e}", EXIT_USAGE)
    if not argv:
        raise Fail("FORGE_GPU_PROBE_CMD is empty", EXIT_USAGE)
    try:
        timeout = float(os.environ.get("FORGE_GPU_PROBE_TIMEOUT", "5"))
    except ValueError:
        timeout = 5.0
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = 5.0
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        raise Fail(f"GPU driver probe could not start: {e}", EXIT_UNREACHABLE)
    deadline = time.monotonic() + timeout
    while p.poll() is None:
        if time.monotonic() >= deadline:
            try:
                os.killpg(p.pid, signal.SIGKILL)  # no-op on an unkillable D-state child; never waited on
            except OSError:
                pass
            raise Fail(f"the GPU driver is not responding (probe did not finish in {timeout:g}s)",
                       EXIT_UNREACHABLE)
        time.sleep(0.05)
    if p.returncode != 0:
        raise Fail(f"the GPU driver is not responding (probe exited {p.returncode})", EXIT_UNREACHABLE)


def blender_cmd(script: Path, args: list[str]) -> list[str]:
    return ["blender", "-b", "--factory-startup", "--python", str(script), "--", *args]


def network_wrapper() -> tuple[list[str], str]:
    """Return (prefix, isolation) for running a command without network."""
    mode = os.environ.get("FORGE_NETNS", "auto")
    if mode == "off":
        return [], "none (FORGE_NETNS=off)"
    unshare = shutil.which("unshare")
    if unshare:
        r = subprocess.run([unshare, "-rn", "true"], capture_output=True)
        if r.returncode == 0:
            return [unshare, "-rn"], "unshare -rn"
    return [], "none (unshare -rn unavailable; script ran WITH network access)"


def cmd_blend(a) -> int:
    script, out = Path(a.script).resolve(), Path(a.out).resolve()
    if not script.is_file():
        raise Fail(f"script not found: {script}", EXIT_USAGE)
    if not shutil.which("blender"):
        raise Fail("blender not found on PATH", EXIT_UNREACHABLE)
    gpu_probe()
    out.mkdir(parents=True, exist_ok=True)
    glb = out / "model.glb"
    if glb.exists():
        glb.unlink()  # a stale export must not pass for this run's
    prefix, isolation = network_wrapper()
    env = dict(os.environ, FORGE_OUT=str(out))
    res: dict = {"out": str(out), "network_isolation": isolation, "timings": {}}

    rc, so, se, to, secs = run_tool(prefix + blender_cmd(script, ["--out", str(out)]), a.timeout, cwd=out, env=env)
    res["timings"]["script_seconds"] = secs
    (out / "blend.log").write_text(so + "\n--- stderr ---\n" + se, encoding="utf-8")
    res["log"] = str(out / "blend.log")
    if to:
        return emit({**res, "ok": False, "reason": f"script timed out after {a.timeout}s"}, EXIT_NEGATIVE)
    if rc != 0:
        return emit({**res, "ok": False, "reason": f"blender exited {rc}", "stderr_tail": tail(se)}, EXIT_NEGATIVE)
    if not glb.is_file():
        return emit({**res, "ok": False, "reason": "script did not export model.glb into the out dir "
                     "(read env FORGE_OUT or the --out argument after `--`)", "stderr_tail": tail(se)},
                    EXIT_NEGATIVE)
    res["glb"] = str(glb)

    rargs = ["--glb", str(glb), "--out", str(out), "--cell", str(a.cell), "--samples", str(a.samples)]
    rc, so, se, to, secs = run_tool(prefix + blender_cmd(HERE / "forge_render.py", rargs), a.timeout, cwd=out, env=env)
    res["timings"]["render_seconds"] = secs
    (out / "render.log").write_text(so + "\n--- stderr ---\n" + se, encoding="utf-8")
    sheet, info_p = out / "sheet.png", out / "render_info.json"
    if to or rc != 0 or not sheet.is_file() or not info_p.is_file():
        why = f"render timed out after {a.timeout}s" if to else f"render failed (blender exit {rc})"
        return emit({**res, "ok": False, "reason": why, "stderr_tail": tail(se), "log": str(out / "render.log")},
                    EXIT_NEGATIVE)
    try:
        check_png(sheet)
        info = json.loads(info_p.read_text(encoding="utf-8"))
    except (Fail, ValueError) as e:
        return emit({**res, "ok": False, "reason": f"render output invalid: {e}"}, EXIT_NEGATIVE)
    res.update({"ok": True, "sheet": str(sheet), "polycount": info.get("triangles"),
                "bounds": {"min": info.get("bounds_min"), "max": info.get("bounds_max"),
                           "extent": info.get("extent")},
                "engine": info.get("engine"), "views": info.get("views"),
                "view_dir": str(out / "views")})
    return emit(res, EXIT_OK)


# ---------------------------------------------------------------- gate

def cmd_gate(a) -> int:
    glb, prof = Path(a.glb).resolve(), Path(a.profile).resolve()
    gate = os.environ.get("FORGE_MESH_GATE")
    if not gate:
        return emit({"ok": None, "status": "skipped",
                     "reason": "FORGE_MESH_GATE is not set; point it at mesh_gate.py"}, EXIT_OK)
    if not Path(gate).is_file():
        raise Fail(f"FORGE_MESH_GATE does not exist: {gate}", EXIT_USAGE)
    for p in (glb, prof):
        if not p.is_file():
            raise Fail(f"not found: {p}", EXIT_USAGE)
    gpu_probe()
    rc, so, se, to, secs = run_tool([sys.executable, gate, str(glb), "--profile", str(prof)], a.timeout)
    if to:
        raise Fail(f"mesh_gate timed out after {a.timeout}s")
    try:
        obj = json.loads(so)
    except ValueError:
        obj = None
    if obj is None:
        code = EXIT_UNREACHABLE if rc == 3 else (EXIT_USAGE if rc == 2 else EXIT_NEGATIVE)
        return emit({"ok": False, "status": "error", "mesh_gate_exit": rc, "stderr_tail": tail(se)}, code)
    return emit(obj, rc if rc in (0, 1, 2, 3) else EXIT_NEGATIVE)


# ---------------------------------------------------------------- compare

def cmd_compare(a) -> int:
    sheet, refdir = Path(a.sheet).resolve(), Path(a.ref_dir).resolve()
    if not refdir.is_dir():
        raise Fail(f"ref dir not found: {refdir}", EXIT_USAGE)
    refs = sorted(refdir.glob("ref_*.png"))
    if not refs:
        raise Fail(f"no ref_*.png in {refdir}")
    check_png(sheet)
    for r in refs:
        check_png(r)
    if not shutil.which("blender"):
        raise Fail("blender not found on PATH", EXIT_UNREACHABLE)
    gpu_probe()
    out = Path(a.out).resolve() if a.out else sheet.with_name("compare.png")
    cargs = ["--sheet", str(sheet), "--refs", ",".join(str(r) for r in refs), "--out", str(out)]
    rc, so, se, to, secs = run_tool(blender_cmd(HERE / "forge_compose.py", cargs), a.timeout)
    if to or rc != 0 or not out.is_file():
        return emit({"ok": False, "reason": "compare timed out" if to else f"blender exit {rc}",
                     "stderr_tail": tail(se)}, EXIT_NEGATIVE)
    w, h = check_png(out)
    return emit({"ok": True, "compare": str(out), "width": w, "height": h,
                 "refs": [str(r) for r in refs], "seconds": secs}, EXIT_OK)


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forge", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("ref", help="generate reference images with agy")
    r.add_argument("--prompt-file", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--n", type=int, default=1)
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--timeout", type=float, default=600, help="seconds per image")
    b = sub.add_parser("blend", help="run a bpy script headless, then render the review sheet")
    b.add_argument("--script", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--timeout", type=float, default=300, help="seconds per blender run")
    b.add_argument("--cell", type=int, default=384)
    b.add_argument("--samples", type=int, default=16, help="sample cap if Cycles is the fallback engine")
    g = sub.add_parser("gate", help="run mesh_gate (FORGE_MESH_GATE) on a glb")
    g.add_argument("glb")
    g.add_argument("--profile", required=True)
    g.add_argument("--timeout", type=float, default=120)
    c = sub.add_parser("compare", help="side-by-side of reference images and the render sheet")
    c.add_argument("sheet")
    c.add_argument("ref_dir")
    c.add_argument("--out")
    c.add_argument("--timeout", type=float, default=120)
    return p


def main(argv=None) -> int:
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    try:
        return {"ref": cmd_ref, "blend": cmd_blend, "gate": cmd_gate, "compare": cmd_compare}[a.cmd](a)
    except Fail as e:
        print(f"forge: {e}", file=sys.stderr)
        return emit({"ok": False, "reason": str(e), **e.extra}, e.code)


if __name__ == "__main__":
    sys.exit(main())
