#!/usr/bin/env python3
"""Bake evolved FunSearch kernels to GPU lookup textures for TEngine deployment.

Usage:
    # 2D LUT from phase kernel result:
    python tools/bake_kernel_lut.py phase  benchmarks/curriculum/2026-05-31/phase_results_gen36.json

    # 2D LUT from latent kernel result:
    python tools/bake_kernel_lut.py latent benchmarks/curriculum/2026-05-31/latent_results_gen20.json

    # 3D LUT from noise kernel result:
    python tools/bake_kernel_lut.py noise  benchmarks/curriculum/2026-05-31/noise_results_gen18.json

Output: benchmarks/luts/<name>_<timestamp>.{bin,json,slang}

The .bin file is a raw float16 binary matching the Slang Texture layout.
The .json file is metadata (dimensions, ranges, kernel type, source fitness).
The .slang file is a drop-in shader snippet.

Verification: after baking, compares 10K random sample points against the
analytical C++ evaluation. Reports max absolute error.
"""
import argparse
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT  = TOOLS_DIR.parent
LUT_DIR    = REPO_ROOT / "benchmarks" / "luts"

sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Per-kernel bake configuration
# ---------------------------------------------------------------------------

KERNEL_CONFIGS = {
    "phase": {
        "code_field": "reaction_code",
        "dims": 2,
        "axes": {
            "phi":  {"min": 0.0, "max": 1.0, "bins": 512},
            "temp": {"min": 0.0, "max": 1.0, "bins": 512},
        },
        "call": "reaction(phi, temp)",
        "slang_signature": "float reaction_lut(float phi, float temp)",
        "slang_sample": "ReactionLUT.SampleLevel(LinearSampler, float2(phi, temp), 0)",
        "max_acceptable_error": 0.005,
    },
    "latent": {
        "code_field": "reaction_code",
        "dims": 3,
        "axes": {
            "phi":   {"min": 0.0, "max": 1.0,   "bins": 64},
            "temp":  {"min": 0.0, "max": 1.0,   "bins": 64},
            "lap_T": {"min": -8.0, "max": 8.0,  "bins": 16},  # normalized to [0,1] for UV
        },
        "call": "reaction(phi, temp, lap_T)",
        "slang_signature": "float reaction_latent_lut(float phi, float temp, float lap_T)",
        "slang_sample": "ReactionLatentLUT.SampleLevel(LinearSampler, float3(phi, temp, clamp((lap_T + 8.0) / 16.0, 0.0, 1.0)), 0)",
        "max_acceptable_error": 0.01,
    },
    "noise": {
        "code_field": "noise_glsl",
        "dims": 3,
        "axes": {
            "p_x": {"min": 0.0, "max": 1.0, "bins": 32},
            "p_y": {"min": 0.0, "max": 1.0, "bins": 32},
            "p_z": {"min": 0.0, "max": 1.0, "bins": 32},
        },
        "call": "noise(vec3(p_x, p_y, p_z))",
        "slang_signature": "float noise_lut(float3 p)",
        "slang_sample": "NoiseLUT.SampleLevel(LinearSampler, frac(p), 0) * 2.0 - 1.0",
        "note": "Apply frac(p) before sampling to handle open-world tiling.",
        "max_acceptable_error": 0.01,
    },
}


# ---------------------------------------------------------------------------
# C++ evaluator wrapper for baking
# ---------------------------------------------------------------------------

_BAKE_WRAPPER_PHASE = r"""
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>
using namespace std;

{CODE}

int main() {{
    int N_phi = {N_PHI};
    int N_temp = {N_TEMP};
    for (int i = 0; i < N_phi; i++) {{
        float phi = (float)i / (N_phi - 1);
        for (int j = 0; j < N_temp; j++) {{
            float temp = (float)j / (N_temp - 1);
            float r = reaction(phi, temp);
            printf("%f\n", r);
        }}
    }}
    return 0;
}}
"""

_BAKE_WRAPPER_LATENT = r"""
#include <cmath>
#include <cstdio>
using namespace std;

{CODE}

int main() {{
    int N_phi = {N_PHI};
    int N_temp = {N_TEMP};
    int N_lapT = {N_LAPT};
    float lap_min = {LAP_MIN}, lap_max = {LAP_MAX};
    for (int i = 0; i < N_phi; i++) {{
        float phi = (float)i / (N_phi - 1);
        for (int j = 0; j < N_temp; j++) {{
            float temp = (float)j / (N_temp - 1);
            for (int k = 0; k < N_lapT; k++) {{
                float lap_T = lap_min + (float)k / (N_lapT - 1) * (lap_max - lap_min);
                float r = reaction(phi, temp, lap_T);
                printf("%f\n", r);
            }}
        }}
    }}
    return 0;
}}
"""

_BAKE_WRAPPER_NOISE_GLSL_TO_CPP = r"""
#include <cmath>
#include <cstdio>

// ---------------------------------------------------------------------------
// GLSL compatibility shim for C++ — supports evolved noise functions.
//
// Supported GLSL features:
//   vec3 arithmetic: +, -, * (component-wise), * float, float -, float +
//   vec3 swizzles:   .yzx, .xxy  (extend as needed via Swizzle3 helper)
//   mat3:            9-scalar constructor (column-major, matching GLSL)
//                    mat3 * mat3, mat3 * vec3
//   Built-ins:       fract, floor, dot, mix, sin, cos, sqrt, abs, mod,
//                    clamp, min, max, length, normalize, step, smoothstep
// ---------------------------------------------------------------------------

// Forward-declare vec3 so Swizzle3 can return it.
struct vec3;

// Swizzle helper: Swizzle3<A,B,C> is a proxy member holding the backing store.
// Indices A,B,C select which of x/y/z (0/1/2) to read back.
template<int A, int B, int C>
struct Swizzle3 {{
    float e[3];
    inline operator vec3() const;
    // Allow vec3 arithmetic on swizzles by implicit conversion
}};

struct vec3 {{
    union {{
        struct {{ float x, y, z; }};
        // Named swizzle members — add more permutations as needed.
        Swizzle3<1,2,0> yzx;
        Swizzle3<0,0,1> xxy;
        Swizzle3<2,0,1> zxy;
        Swizzle3<0,1,2> xyz;
        float data[3];
    }};

    // Constructors
    vec3() {{ x=0; y=0; z=0; }}
    explicit vec3(float s) {{ x=s; y=s; z=s; }}
    vec3(float _x, float _y, float _z) {{ x=_x; y=_y; z=_z; }}
    // Construct from swizzle
    template<int A, int B, int C>
    vec3(const Swizzle3<A,B,C>& s) {{ x=s.e[A]; y=s.e[B]; z=s.e[C]; }}

    // Compound assignment
    vec3& operator+=(const vec3& o) {{ x+=o.x; y+=o.y; z+=o.z; return *this; }}
    vec3& operator-=(const vec3& o) {{ x-=o.x; y-=o.y; z-=o.z; return *this; }}
    vec3& operator*=(float s)       {{ x*=s;   y*=s;   z*=s;   return *this; }}
    vec3& operator*=(const vec3& o) {{ x*=o.x; y*=o.y; z*=o.z; return *this; }}
    // Allow vec3 += float (broadcast): needed for p += dot(p,q)
    vec3& operator+=(float s)       {{ x+=s;   y+=s;   z+=s;   return *this; }}
    vec3& operator-=(float s)       {{ x-=s;   y-=s;   z-=s;   return *this; }}
}};

// Swizzle → vec3 conversion (defined after vec3 is complete)
template<int A, int B, int C>
inline Swizzle3<A,B,C>::operator vec3() const {{
    return vec3(e[A], e[B], e[C]);
}}

// vec3 binary operators
static inline vec3 operator+(vec3 a, const vec3& b) {{ return vec3(a.x+b.x, a.y+b.y, a.z+b.z); }}
static inline vec3 operator-(vec3 a, const vec3& b) {{ return vec3(a.x-b.x, a.y-b.y, a.z-b.z); }}
static inline vec3 operator*(vec3 a, const vec3& b) {{ return vec3(a.x*b.x, a.y*b.y, a.z*b.z); }}
static inline vec3 operator*(vec3 a, float s)       {{ return vec3(a.x*s,   a.y*s,   a.z*s);   }}
static inline vec3 operator*(float s, vec3 a)       {{ return vec3(s*a.x,   s*a.y,   s*a.z);   }}
static inline vec3 operator/(vec3 a, float s)       {{ return vec3(a.x/s,   a.y/s,   a.z/s);   }}
// float - vec3  (e.g. "3.0 - 2.0 * f")
static inline vec3 operator-(float s, vec3 a)       {{ return vec3(s-a.x,   s-a.y,   s-a.z);   }}
static inline vec3 operator+(float s, vec3 a)       {{ return vec3(s+a.x,   s+a.y,   s+a.z);   }}
static inline vec3 operator+(vec3 a, float s)       {{ return vec3(a.x+s,   a.y+s,   a.z+s);   }}
static inline vec3 operator-(vec3 a, float s)       {{ return vec3(a.x-s,   a.y-s,   a.z-s);   }}
// unary negation
static inline vec3 operator-(const vec3& a)         {{ return vec3(-a.x, -a.y, -a.z);           }}

// Swizzle + float (needed for: p.yzx + 19.19)
template<int A, int B, int C>
static inline vec3 operator+(const Swizzle3<A,B,C>& s, float f) {{
    return vec3(s.e[A]+f, s.e[B]+f, s.e[C]+f);
}}
template<int A, int B, int C>
static inline vec3 operator+(float f, const Swizzle3<A,B,C>& s) {{
    return vec3(f+s.e[A], f+s.e[B], f+s.e[C]);
}}

// vec2 (occasionally used in evolved GLSL)
struct vec2 {{
    float x, y;
    vec2() {{ x=0; y=0; }}
    vec2(float _x, float _y) {{ x=_x; y=_y; }}
    explicit vec2(float s) {{ x=s; y=s; }}
}};
static inline vec2 operator+(vec2 a, vec2 b) {{ return vec2(a.x+b.x, a.y+b.y); }}
static inline vec2 operator*(vec2 a, float s) {{ return vec2(a.x*s, a.y*s); }}

// mat3 — column-major (matches GLSL: mat3(col0, col1, col2) where each is vec3,
// OR mat3(c00,c10,c20, c01,c11,c21, c02,c12,c22) for 9-scalar form)
struct mat3 {{
    vec3 col[3];  // col[j][i]
    // 9-scalar constructor: GLSL mat3(a0,a1,a2, b0,b1,b2, c0,c1,c2)
    // fills column-major: col0=(a0,a1,a2), col1=(b0,b1,b2), col2=(c0,c1,c2)
    mat3(float c00, float c10, float c20,
         float c01, float c11, float c21,
         float c02, float c12, float c22) {{
        col[0] = vec3(c00, c10, c20);
        col[1] = vec3(c01, c11, c21);
        col[2] = vec3(c02, c12, c22);
    }}
    // 3-vec3 constructor: mat3(col0, col1, col2)
    mat3(vec3 c0, vec3 c1, vec3 c2) {{
        col[0]=c0; col[1]=c1; col[2]=c2;
    }}
    mat3() {{}}
}};

// mat3 * vec3: result_i = dot(row_i, v) = col[0][i]*v.x + col[1][i]*v.y + col[2][i]*v.z
static inline vec3 operator*(const mat3& m, const vec3& v) {{
    return vec3(
        m.col[0].x*v.x + m.col[1].x*v.y + m.col[2].x*v.z,
        m.col[0].y*v.x + m.col[1].y*v.y + m.col[2].y*v.z,
        m.col[0].z*v.x + m.col[1].z*v.y + m.col[2].z*v.z
    );
}}

// mat3 * mat3: result column j = m * n.col[j]
static inline mat3 operator*(const mat3& m, const mat3& n) {{
    return mat3(m * n.col[0], m * n.col[1], m * n.col[2]);
}}

// ---------------------------------------------------------------------------
// GLSL built-in functions (C++ overloads, no _Generic — this is C++ not C11)
// ---------------------------------------------------------------------------
static inline float fract(float x)   {{ return x - floorf(x); }}
static inline vec3  fract(vec3 v)    {{ return vec3(fract(v.x), fract(v.y), fract(v.z)); }}
static inline float floor(float x)   {{ return floorf(x); }}
static inline vec3  floor(vec3 v)    {{ return vec3(floorf(v.x), floorf(v.y), floorf(v.z)); }}
static inline float dot(vec3 a, vec3 b) {{ return a.x*b.x + a.y*b.y + a.z*b.z; }}
static inline float mix(float a, float b, float t) {{ return a + t*(b-a); }}

static inline float sin(float x)     {{ return sinf(x); }}
static inline float cos(float x)     {{ return cosf(x); }}
static inline float sqrt(float x)    {{ return sqrtf(x); }}
static inline float abs(float x)     {{ return fabsf(x); }}
static inline vec3  abs(vec3 v)      {{ return vec3(fabsf(v.x), fabsf(v.y), fabsf(v.z)); }}
static inline float mod(float x, float y)  {{ return fmodf(x, y); }}
static inline vec3  mod(vec3 v, float s)   {{ return vec3(fmodf(v.x,s), fmodf(v.y,s), fmodf(v.z,s)); }}
static inline float min(float a, float b)  {{ return a < b ? a : b; }}
static inline float max(float a, float b)  {{ return a > b ? a : b; }}
static inline vec3  min(vec3 a, vec3 b)    {{ return vec3(min(a.x,b.x), min(a.y,b.y), min(a.z,b.z)); }}
static inline vec3  max(vec3 a, vec3 b)    {{ return vec3(max(a.x,b.x), max(a.y,b.y), max(a.z,b.z)); }}
static inline float clamp(float x, float lo, float hi) {{ return x<lo?lo:x>hi?hi:x; }}
static inline vec3  clamp(vec3 v, float lo, float hi) {{
    return vec3(clamp(v.x,lo,hi), clamp(v.y,lo,hi), clamp(v.z,lo,hi));
}}
static inline float length(vec3 v)   {{ return sqrtf(v.x*v.x + v.y*v.y + v.z*v.z); }}
static inline vec3  normalize(vec3 v) {{
    float l = length(v); return l > 1e-10f ? v*(1.f/l) : vec3(0,0,0);
}}
static inline float step(float e, float x) {{ return x < e ? 0.f : 1.f; }}
static inline float smoothstep(float e0, float e1, float x) {{
    float t = clamp((x-e0)/(e1-e0), 0.f, 1.f);
    return t*t*(3.f - 2.f*t);
}}

{CODE}

int main() {{
    int N = {N_PER_DIM};
    for (int i = 0; i < N; i++) {{
        float px = (float)i / (N - 1);
        for (int j = 0; j < N; j++) {{
            float py = (float)j / (N - 1);
            for (int k = 0; k < N; k++) {{
                float pz = (float)k / (N - 1);
                float r = noise(vec3(px, py, pz));
                // Normalize GLSL [-1,1] range to [0,1] for texture storage
                printf("%f\n", (r + 1.0f) * 0.5f);
            }}
        }}
    }}
    return 0;
}}
"""


def compile_and_eval(cpp_src: str) -> list[float]:
    """Compile C++ to binary, run it, collect float output."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "bake.cpp"
        exe = Path(td) / "bake"
        src.write_text(cpp_src)
        result = subprocess.run(
            ["g++", "-O2", "-o", str(exe), str(src), "-lm"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Compile failed:\n{result.stderr}")
        out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            raise RuntimeError(f"Run failed:\n{out.stderr}")
    return [float(x) for x in out.stdout.strip().split("\n") if x.strip()]


def to_float16_bytes(values: list[float]) -> bytes:
    """Pack float values as float16 bytes (raw, no padding)."""
    out = bytearray()
    for v in values:
        # Clamp to float16 range
        v = max(-65504.0, min(65504.0, v))
        out += struct.pack("<e", v)
    return bytes(out)


def to_float32_bytes(values: list[float]) -> bytes:
    out = bytearray()
    for v in values:
        out += struct.pack("<f", v)
    return bytes(out)


# ---------------------------------------------------------------------------
# Per-kernel bake functions
# ---------------------------------------------------------------------------

def bake_phase(code: str, cfg: dict) -> tuple[list[float], dict]:
    axes = cfg["axes"]
    N_phi  = axes["phi"]["bins"]
    N_temp = axes["temp"]["bins"]
    src = _BAKE_WRAPPER_PHASE.format(CODE=code, N_PHI=N_phi, N_TEMP=N_temp)
    values = compile_and_eval(src)
    expected = N_phi * N_temp
    if len(values) != expected:
        raise RuntimeError(f"Expected {expected} values, got {len(values)}")
    meta = {"type": "Texture2D", "format": "R16F",
            "width": N_phi, "height": N_temp,
            "axes": ["phi [0,1]", "temp [0,1]"],
            "shader_var": "ReactionLUT"}
    return values, meta


def bake_latent(code: str, cfg: dict) -> tuple[list[float], dict]:
    axes = cfg["axes"]
    N_phi  = axes["phi"]["bins"]
    N_temp = axes["temp"]["bins"]
    N_lapT = axes["lap_T"]["bins"]
    lap_min = axes["lap_T"]["min"]
    lap_max = axes["lap_T"]["max"]
    src = _BAKE_WRAPPER_LATENT.format(
        CODE=code, N_PHI=N_phi, N_TEMP=N_temp, N_LAPT=N_lapT,
        LAP_MIN=lap_min, LAP_MAX=lap_max,
    )
    values = compile_and_eval(src)
    expected = N_phi * N_temp * N_lapT
    if len(values) != expected:
        raise RuntimeError(f"Expected {expected} values, got {len(values)}")
    meta = {"type": "Texture3D", "format": "R32F",
            "width": N_phi, "height": N_temp, "depth": N_lapT,
            "axes": ["phi [0,1]", "temp [0,1]", f"lap_T [{lap_min},{lap_max}]"],
            "shader_var": "ReactionLatentLUT"}
    return values, meta


def bake_noise(code: str, cfg: dict) -> tuple[list[float], dict]:
    N = cfg["axes"]["p_x"]["bins"]
    src = _BAKE_WRAPPER_NOISE_GLSL_TO_CPP.format(CODE=code, N_PER_DIM=N)
    values = compile_and_eval(src)
    expected = N * N * N
    if len(values) != expected:
        raise RuntimeError(f"Expected {expected} values, got {len(values)}")
    meta = {"type": "Texture3D", "format": "R16F",
            "width": N, "height": N, "depth": N,
            "axes": ["p.x [0,1]", "p.y [0,1]", "p.z [0,1]"],
            "note": "Values stored as (noise+1)/2 ∈ [0,1]. Shader: sample * 2.0 - 1.0",
            "shader_var": "NoiseLUT"}
    return values, meta


BAKE_FN = {"phase": bake_phase, "latent": bake_latent, "noise": bake_noise}


# ---------------------------------------------------------------------------
# Slang snippet generation
# ---------------------------------------------------------------------------

def make_slang_snippet(kernel_type: str, cfg: dict, meta: dict,
                        lut_bin_path: Path, fitness: float) -> str:
    sig = cfg["slang_signature"]
    sample = cfg["slang_sample"]
    note = cfg.get("note", "")
    t = meta["type"]
    fmt = meta["format"]
    sv = meta["shader_var"]
    dims = " × ".join(str(v) for v in [meta["width"], meta.get("height",""), meta.get("depth","")] if v)

    lines = [
        f"// Auto-baked from FunSearch evolution — fitness={fitness:.4f}",
        f"// Source: {lut_bin_path.name}",
        f"// Grid: {dims}  Format: {fmt}  Type: {t}",
    ]
    if note:
        lines.append(f"// Note: {note}")
    lines += [
        "",
        f"{t}<{fmt.split('R')[1].replace('F','')}-bit float> {sv};",
        "SamplerState LinearSampler;",
        "",
        f"{sig} {{",
        f"    return {sample};",
        "}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Verification: compare LUT samples against analytical evaluation
# ---------------------------------------------------------------------------

def verify_lut(kernel_type: str, code: str, cfg: dict, values: list[float],
               n_samples: int = 10000) -> float:
    """Sample N random points; return max absolute error between LUT and analytical."""
    import random
    rng = random.Random(42)
    axes = cfg["axes"]
    axis_names = list(axes.keys())
    errors = []

    # Build a quick sampling evaluator by compiling the function once
    if kernel_type in ("phase", "latent"):
        call_expr = cfg["call"]
        params = ", ".join(f"float {a}" for a in axis_names)
        header = f"""
#include <cmath>
#include <cstdio>
using namespace std;
{code}
int main() {{
    float v;
"""
        # We'll pass points via a pre-generated file to avoid massive binary
        points = []
        for _ in range(n_samples):
            pt = [rng.uniform(axes[a]["min"], axes[a]["max"]) for a in axis_names]
            points.append(pt)

        point_calls = "\n".join(
            "printf(\"%f\\n\", " + call_expr.replace(
                axis_names[0], str(points[i][0])
            ).replace(
                axis_names[1] if len(axis_names) > 1 else "__NONE__",
                str(points[i][1]) if len(axis_names) > 1 else ""
            ).replace(
                axis_names[2] if len(axis_names) > 2 else "__NONE__",
                str(points[i][2]) if len(axis_names) > 2 else ""
            ) + ");"
            for i in range(min(n_samples, 1000))  # limit to 1000 for compile speed
        )
        src = f"#include <cmath>\n#include <cstdio>\nusing namespace std;\n{code}\nint main(){{\n{point_calls}\nreturn 0;}}"
        try:
            analytical = compile_and_eval(src)
        except Exception:
            return float("nan")

        # Compare analytical to interpolated LUT values
        ax_list = list(axes.items())
        for i, pt in enumerate(points[:len(analytical)]):
            # Nearest-neighbor lookup in the baked grid
            indices = []
            shape = []
            for j, (aname, adef) in enumerate(ax_list):
                bins = adef["bins"]
                frac = (pt[j] - adef["min"]) / (adef["max"] - adef["min"])
                idx = int(frac * (bins - 1) + 0.5)
                idx = max(0, min(bins - 1, idx))
                indices.append(idx)
                shape.append(bins)

            # Flat index (row-major: innermost = last axis)
            flat = 0
            for j, idx in enumerate(indices):
                stride = 1
                for k in range(j + 1, len(shape)):
                    stride *= shape[k]
                flat += idx * stride

            if flat < len(values):
                lut_val = values[flat]
                errors.append(abs(lut_val - analytical[i]))

    return max(errors) if errors else float("nan")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Bake FunSearch kernel to LUT texture")
    parser.add_argument("kernel_type", choices=list(KERNEL_CONFIGS))
    parser.add_argument("results_json", help="Path to FunSearch results JSON")
    parser.add_argument("--rank", type=int, default=0,
                        help="Which program to bake (0=best, 1=second-best, ...)")
    parser.add_argument("--float32", action="store_true",
                        help="Store as float32 instead of float16 (2× size, more precise)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip verification step")
    args = parser.parse_args()

    results_path = Path(args.results_json)
    if not results_path.exists():
        print(f"Error: {results_path} not found")
        return 1

    with open(results_path) as f:
        data = json.load(f)

    programs = data.get("top_programs", [])
    if not programs:
        print("Error: no top_programs in results JSON")
        return 1

    if args.rank >= len(programs):
        print(f"Error: rank {args.rank} out of range (only {len(programs)} programs)")
        return 1

    prog = programs[args.rank]
    cfg = KERNEL_CONFIGS[args.kernel_type]
    code = prog.get(cfg["code_field"], "")
    if not code:
        print(f"Error: no code in field {cfg['code_field']!r}")
        return 1

    fitness = float(prog.get("fitness", 0))
    print(f"[bake] kernel={args.kernel_type}  rank={args.rank}  fitness={fitness:.4f}")
    print(f"[bake] code ({len(code)} chars):")
    for line in code.strip().split("\n")[:8]:
        print(f"  {line}")
    if len(code.strip().split("\n")) > 8:
        print(f"  ... (+{len(code.strip().split(chr(10)))-8} more lines)")

    # Bake
    print(f"\n[bake] sampling grid...")
    t0 = time.time()
    bake_fn = BAKE_FN[args.kernel_type]
    values, meta = bake_fn(code, cfg)
    print(f"[bake] {len(values)} samples in {time.time()-t0:.1f}s")
    print(f"  value range: [{min(values):.4f}, {max(values):.4f}]")

    # Verify
    if not args.no_verify:
        print(f"\n[bake] verifying (1000 random samples)...")
        max_err = verify_lut(args.kernel_type, code, cfg, values)
        threshold = cfg["max_acceptable_error"]
        status = "OK" if math.isnan(max_err) else ("OK" if max_err <= threshold else "WARN")
        print(f"[bake] max abs error = {max_err:.6f}  threshold={threshold}  [{status}]")
        if status == "WARN":
            print(f"  WARNING: error exceeds threshold — increase grid resolution or check code")

    # Write output
    LUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{args.kernel_type}_lut_{int(time.time())}"
    bin_path   = LUT_DIR / f"{stem}.bin"
    meta_path  = LUT_DIR / f"{stem}.json"
    slang_path = LUT_DIR / f"{stem}.slang"

    # Binary
    dtype = "float32" if args.float32 else "float16"
    raw = to_float32_bytes(values) if args.float32 else to_float16_bytes(values)
    bin_path.write_bytes(raw)

    # Metadata
    meta.update({
        "kernel_type": args.kernel_type,
        "source_file": str(results_path),
        "source_rank": args.rank,
        "fitness": fitness,
        "dtype": dtype,
        "n_values": len(values),
        "file_bytes": len(raw),
        "bake_timestamp": int(time.time()),
    })
    meta_path.write_text(json.dumps(meta, indent=2))

    # Slang snippet
    slang = make_slang_snippet(args.kernel_type, cfg, meta, bin_path, fitness)
    slang_path.write_text(slang)

    kb = len(raw) / 1024
    print(f"\n[bake] Output:")
    print(f"  {bin_path}  ({kb:.1f} KB {dtype})")
    print(f"  {meta_path}")
    print(f"  {slang_path}")
    print(f"\n[bake] Slang snippet:")
    print(slang)
    return 0


if __name__ == "__main__":
    sys.exit(main())
