"""render_vault_shaders — render evolved SDF programs to PNG via CPU tracer.

Primary mode: reads C++ sdf() directly from a results JSON (zero translation loss).
Secondary mode: pulls GLSL shaders from the vault API (best-effort GLSL→C++ conversion).

Usage:
    # Render top-5 from a results file (cleanest path):
    python3 tools/render_vault_shaders.py --results autobench/benchmarks/curriculum/2026-05-30/sdf_results_gen60.json

    # Render whatever is in the vault right now:
    python3 tools/render_vault_shaders.py --vault

    # Both:
    python3 tools/render_vault_shaders.py --results path/to/results.json --vault

    # Larger renders:
    python3 tools/render_vault_shaders.py --results path.json --size 768
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

DASHBOARD_URL = "http://localhost:9104"

# Per-instance camera orbit distance (matches playground_push.py)
_CAM_DIST: dict[str, float] = {
    "gyroid":        3.0,
    "round_box":     3.0,
    "sphere":        3.0,
    "warped_sphere": 3.0,
    "smooth_union":  3.5,
    "cloud_cluster": 4.5,
    "torus_knot":    2.0,
    "helix_tube":    2.5,
    "scherk_first":  2.2,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cam(instance: str) -> float:
    return _CAM_DIST.get(instance, 3.5)


def _render_cpp(cpp_code: str, out_path: Path, size: int, instance: str) -> bool:
    from autobench.sdf_tracer import render_sdf_cpp_to_png
    return render_sdf_cpp_to_png(
        cpp_code, out_path,
        viewport=(size, size),
        camera_dist=_cam(instance),
        render_timeout=120.0,   # allow slow SDFs up to 2 min
        compile_timeout=30.0,
    )


def _glsl_sdf_to_cpp(glsl_sdf: str) -> str | None:
    """Best-effort GLSL sdf(vec3) → C++ extern "C" sdf(float x,y,z) translation.

    Only handles the patterns produced by cpp_to_glsl() — this is a round-trip
    reverse, not a general GLSL→C++ transpiler. Assumes the input uses only
    scalar GLSL math (sin, cos, sqrt, abs, max, min, pow, exp, floor, mod, mix,
    clamp, length scalar only). Returns None if vec3 constructor / swizzle usage
    is detected (those require glm or a vec3 shim to handle safely).
    """
    code = glsl_sdf

    # Reject if there are vec3/vec2 variables in the body (beyond the signature)
    body_start = code.find('{')
    body = code[body_start:] if body_start != -1 else code
    if re.search(r'\b(?:vec3|vec2|vec4)\s+\w+', body):
        return None  # vec types in body — unsafe without a shim

    # Signature
    code = re.sub(
        r'float\s+sdf\s*\(\s*vec3\s+\w+\s*\)',
        'extern "C" float sdf(float x, float y, float z)',
        code,
    )
    # Remove coord unpacking line (x = pos.x etc.) — params are already x,y,z
    code = re.sub(r'\n[^\n]*float\s+x\s*=\s*\w+\.x[^\n]*\n', '\n', code)

    # GLSL built-ins → C++ stdlib
    renames = [
        ('sqrt(', 'sqrtf('), ('abs(', 'fabsf('), ('max(', 'fmaxf('), ('min(', 'fminf('),
        ('sin(', 'sinf('),   ('cos(', 'cosf('),   ('atan(', 'atan2f('), ('pow(', 'powf('),
        ('exp(', 'expf('),   ('log(', 'logf('),   ('floor(', 'floorf('), ('ceil(', 'ceilf('),
        ('round(', 'roundf('), ('mod(', 'fmodf('), ('sign(', 'sign_f('),
        ('mix(',   '_mix('),  ('clamp(', '_clamp('), ('length(', '_len('),
        ('smoothstep(', '_sstep('),
    ]
    for glsl, cpp in renames:
        code = code.replace(glsl, cpp)

    helpers = """\
#include <cmath>
static inline float _mix(float a, float b, float t) { return a + (b-a)*t; }
static inline float _clamp(float x, float lo, float hi) { return x<lo?lo:x>hi?hi:x; }
static inline float _len(float a) { return fabsf(a); }
static inline float sign_f(float x) { return x>0?1.f:x<0?-1.f:0.f; }
static inline float _sstep(float e0, float e1, float x) {
    float t=_clamp((x-e0)/(e1-e0),0.f,1.f); return t*t*(3.f-2.f*t); }
"""
    return helpers + code


# ---------------------------------------------------------------------------
# Render from results JSON (authoritative C++ path)
# ---------------------------------------------------------------------------

def render_from_results(
    results_path: Path,
    out_dir: Path,
    size: int = 512,
    top_n: int = 5,
) -> list[Path]:
    with open(results_path) as f:
        data = json.load(f)

    run_id = data.get("run_id", results_path.stem)[:12]
    top = data.get("top_programs", [])

    print(f"\n[results] {results_path.name} — {len(top)} programs, run={run_id}")

    rendered: list[Path] = []
    for i, prog in enumerate(top[:top_n]):
        code = prog.get("sdf_code") or prog.get("code", "")
        if not code:
            print(f"  rank{i+1}: no code field — skipping")
            continue

        fitness = prog.get("fitness", 0.0)
        instance = prog.get("instance", "")
        gen = prog.get("generation", 0)

        label = f"rank{i+1:02d}_f{fitness:.4f}_g{gen:03d}_{instance or 'sdf'}"
        out_path = out_dir / f"{label}.png"

        print(f"  rank{i+1}: fitness={fitness:.4f} instance={instance or '?'} gen={gen}", end=" ")
        ok = _render_cpp(code, out_path, size, instance)
        if ok and out_path.exists():
            print(f"→ {out_path.name}")
            rendered.append(out_path)
        else:
            print("FAIL")

    return rendered


# ---------------------------------------------------------------------------
# Render from vault API (GLSL round-trip path, best-effort)
# ---------------------------------------------------------------------------

def _extract_sdf_glsl(vault_code: str) -> str | None:
    """Extract sdf() function from vault shader (strips mainImage)."""
    m = re.search(r'float\s+sdf\s*\(\s*vec3\s+\w+\s*\)\s*\{', vault_code)
    if not m:
        return None
    start = m.start()
    # Find the opening brace, then walk forward until the matching close brace.
    brace_start = vault_code.index('{', m.start())
    depth, i = 0, brace_start
    while i < len(vault_code):
        depth += (vault_code[i] == '{') - (vault_code[i] == '}')
        if depth == 0:
            return vault_code[start:i + 1]
        i += 1
    return None


def render_from_vault(
    out_dir: Path,
    size: int = 512,
    dashboard_url: str = DASHBOARD_URL,
) -> list[Path]:
    try:
        resp = urllib.request.urlopen(f"{dashboard_url}/api/portal/vault/shaders", timeout=10)
        data = json.loads(resp.read())
    except Exception as e:
        print(f"\n[vault] Unreachable: {e}")
        return []

    shaders = data.get("shaders", data) if isinstance(data, dict) else data
    print(f"\n[vault] {len(shaders)} shader(s) at {dashboard_url}")

    rendered: list[Path] = []
    for s in shaders:
        sid = s.get("id", "?")[:16]
        title = s.get("title", "untitled")
        code = s.get("code", "")

        print(f"  [{sid}] {title[:60]}", end=" ")

        sdf_glsl = _extract_sdf_glsl(code)
        if not sdf_glsl:
            print("SKIP: no sdf() found")
            continue

        cpp = _glsl_sdf_to_cpp(sdf_glsl)
        if cpp is None:
            print("SKIP: vec3 body (needs shim)")
            continue

        safe = re.sub(r'[^\w\-]', '_', title)[:60]
        out_path = out_dir / f"vault_{safe}.png"

        # Infer instance from title
        instance = next((k for k in _CAM_DIST if k in title.lower()), "")
        ok = _render_cpp(cpp, out_path, size, instance)
        if ok and out_path.exists():
            print(f"→ {out_path.name}")
            rendered.append(out_path)
        else:
            print("FAIL")

    return rendered


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Render SDF programs to PNG via CPU tracer")
    p.add_argument("--results", metavar="JSON", help="sdf_results_gen*.json (C++ source, cleanest path)")
    p.add_argument("--vault", action="store_true", help="Also render shaders from vault API")
    p.add_argument("--out-dir", default="/tmp/sdf_renders", type=Path)
    p.add_argument("--size", default=512, type=int, help="Render viewport size (default 512)")
    p.add_argument("--top-n", default=5, type=int, help="Top-N from results file (default 5)")
    p.add_argument("--dashboard-url", default=DASHBOARD_URL)
    args = p.parse_args()

    if not args.results and not args.vault:
        p.print_help()
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[Path] = []

    if args.results:
        rendered += render_from_results(Path(args.results), args.out_dir, args.size, args.top_n)

    if args.vault:
        rendered += render_from_vault(args.out_dir, args.size, args.dashboard_url)

    print(f"\nRendered {len(rendered)} PNG(s) to {args.out_dir}")
    for path in rendered:
        print(f"  {path}")
    return 0 if rendered else 1


if __name__ == "__main__":
    raise SystemExit(main())
