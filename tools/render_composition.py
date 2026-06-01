#!/usr/bin/env python3
"""Render composition oracle results as a 4-panel showcase figure.

Shows terrain height, temperature field, initial phi (water), final phi (ice)
for the best (terrain, phase) pair. Saves to benchmarks/luts/composition_showcase.png

Usage:
    python tools/render_composition.py \
        benchmarks/curriculum/2026-05-31/terrain_results_gen17.json \
        benchmarks/curriculum/2026-05-31/phase_results_gen16.json
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors

_RENDER_CPP = r"""
#include <cmath>
#include <cstdio>
#include <string>
using namespace std;

struct vec2 {
    float x, y;
    vec2() : x(0), y(0) {}
    vec2(float x, float y): x(x), y(y) {}
    vec2 operator+(const vec2& o) const { return {x+o.x, y+o.y}; }
    vec2 operator-(const vec2& o) const { return {x-o.x, y-o.y}; }
    vec2 operator*(float s) const { return {x*s, y*s}; }
    vec2 operator*(const vec2& o) const { return {x*o.x, y*o.y}; }
    vec2 operator/(float s) const { return {x/s, y/s}; }
    vec2& operator+=(const vec2& o){ x+=o.x; y+=o.y; return *this; }
    float& operator[](int i) { return i==0?x:y; }
};
static inline vec2 operator*(float s, const vec2& v) { return {s*v.x, s*v.y}; }
static inline vec2 operator+(float s, const vec2& v) { return {s+v.x, s+v.y}; }
static inline vec2 operator-(float s, const vec2& v) { return {s-v.x, s-v.y}; }
static inline float dot(vec2 a, vec2 b) { return a.x*b.x+a.y*b.y; }
static inline float length(vec2 v) { return sqrtf(v.x*v.x+v.y*v.y); }
static inline vec2 normalize(vec2 v) { float l=length(v); return l>1e-9f?v*(1.f/l):vec2(0,0); }
static inline vec2 abs(vec2 v) { return {fabsf(v.x),fabsf(v.y)}; }
static inline vec2 floor(vec2 v) { return {floorf(v.x),floorf(v.y)}; }
static inline vec2 fract(vec2 v) { return {v.x-floorf(v.x),v.y-floorf(v.y)}; }
static inline float fract(float x) { return x-floorf(x); }
static inline vec2 min(vec2 a,vec2 b){ return {fminf(a.x,b.x),fminf(a.y,b.y)}; }
static inline vec2 max(vec2 a,vec2 b){ return {fmaxf(a.x,b.x),fmaxf(a.y,b.y)}; }
static inline vec2 clamp(vec2 v,float a,float b){ return {fmaxf(a,fminf(b,v.x)),fmaxf(a,fminf(b,v.y))}; }
static inline float mix(float a,float b,float t) { return a+t*(b-a); }
static inline vec2 mix(vec2 a,vec2 b,float t) { return {a.x+t*(b.x-a.x),a.y+t*(b.y-a.y)}; }
static inline float clamp(float x,float a,float b) { return fmaxf(a,fminf(b,x)); }
static inline float smoothstep(float e0,float e1,float x) {
    float t=clamp((x-e0)/(e1-e0),0.f,1.f); return t*t*(3.f-2.f*t); }
static inline float sign(float x) { return (x>0.f)-(x<0.f); }
static inline float mod(float x,float y) { return fmodf(x,y); }
static inline float step(float e,float x) { return x>=e?1.f:0.f; }

{TERRAIN_CODE}
{REACTION_CODE}

int main() {
    const int N = {N};
    const float dt = 0.018f;
    const float D  = 10.0f;
    const int n_steps = 150;
    const float T_cold = 0.25f, T_hot = 0.75f;

    float H[N][N];
    float hmin = 1e9f, hmax = -1e9f;
    for(int i=0;i<N;i++) for(int j=0;j<N;j++){
        float px = -1.f + 2.f*(float)j/(N-1);
        float py = -1.f + 2.f*(float)i/(N-1);
        float v = terrain(vec2(px,py));
        H[i][j]=v; hmin=fminf(hmin,v); hmax=fmaxf(hmax,v);
    }
    float hrange = hmax-hmin;
    for(int i=0;i<N;i++) for(int j=0;j<N;j++) H[i][j]=(H[i][j]-hmin)/hrange;

    float phi[N][N], T_field[N][N];
    bool wet_mask[N][N];
    float cx=(N-1)*0.5f, cy=(N-1)*0.5f;
    for(int i=0;i<N;i++) for(int j=0;j<N;j++){
        bool wet = (H[i][j] < 0.4f);
        wet_mask[i][j] = wet;
        phi[i][j] = wet ? 0.05f : 0.0f;
        float r = sqrtf((i-cx)*(i-cx)+(j-cy)*(j-cy));
        T_field[i][j] = (r < (float)N*0.3f) ? T_cold : T_hot;
    }
    for(int di=-2;di<=2;di++) for(int dj=-2;dj<=2;dj++){
        int ii=(int)cx+di, jj=(int)cy+dj;
        if(ii>=0&&ii<N&&jj>=0&&jj<N&&phi[ii][jj]>0.01f) phi[ii][jj]=0.65f;
    }

    // Print initial state
    printf("TERRAIN\n");
    for(int i=0;i<N;i++){for(int j=0;j<N;j++) printf("%.4f ", H[i][j]); printf("\n");}
    printf("TEMPERATURE\n");
    for(int i=0;i<N;i++){for(int j=0;j<N;j++) printf("%.4f ", T_field[i][j]); printf("\n");}
    printf("PHI_INIT\n");
    for(int i=0;i<N;i++){for(int j=0;j<N;j++) printf("%.4f ", phi[i][j]); printf("\n");}

    // Evolve
    float phi_new[N][N];
    for(int s=0;s<n_steps;s++){
        for(int i=0;i<N;i++) for(int j=0;j<N;j++){
            if(!wet_mask[i][j]){ phi_new[i][j]=0.0f; continue; }
            float p=phi[i][j];
            float pL=(i>0   && wet_mask[i-1][j])?phi[i-1][j]:p;
            float pR=(i<N-1 && wet_mask[i+1][j])?phi[i+1][j]:p;
            float pD=(j>0   && wet_mask[i][j-1])?phi[i][j-1]:p;
            float pU=(j<N-1 && wet_mask[i][j+1])?phi[i][j+1]:p;
            float lap=pL+pR+pD+pU-4.f*p;
            float r=reaction(p,T_field[i][j]);
            if(!isfinite(r)||fabsf(r)>50.f) r=0.f;
            phi_new[i][j]=p+dt*(D*lap+r);
            if(!isfinite(phi_new[i][j])) phi_new[i][j]=p;
        }
        for(int i=0;i<N;i++) for(int j=0;j<N;j++) phi[i][j]=phi_new[i][j];
    }

    printf("PHI_FINAL\n");
    for(int i=0;i<N;i++){for(int j=0;j<N;j++) printf("%.4f ", phi[i][j]); printf("\n");}
    return 0;
}
"""


def run_sim(terrain_code: str, reaction_code: str, N: int = 64) -> dict:
    cpp = _RENDER_CPP.replace("{TERRAIN_CODE}", terrain_code)
    cpp = cpp.replace("{REACTION_CODE}", reaction_code)
    cpp = cpp.replace("{N}", str(N))

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "sim.cpp"
        exe = Path(td) / "sim"
        src.write_text(cpp)
        r = subprocess.run(["g++", "-O2", "-o", str(exe), str(src), "-lm"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"Compile failed:\n{r.stderr[:500]}")
        out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(f"Run failed:\n{out.stderr[:200]}")

    lines = out.stdout.strip().split("\n")
    result = {}
    current = None
    rows = []
    for line in lines:
        if line in ("TERRAIN", "TEMPERATURE", "PHI_INIT", "PHI_FINAL"):
            if current and rows:
                result[current] = np.array([list(map(float, r.split())) for r in rows])
            current = line
            rows = []
        else:
            rows.append(line)
    if current and rows:
        result[current] = np.array([list(map(float, r.split())) for r in rows])
    return result


def render(terrain_json: str, phase_json: str, output: str, N: int = 64):
    with open(terrain_json) as f:
        td = json.load(f)
    with open(phase_json) as f:
        pd = json.load(f)

    t_code = td["top_programs"][0]["terrain_code"]
    r_code = pd["top_programs"][0]["reaction_code"]
    terrain_fit = td["top_programs"][0]["fitness"]
    phase_fit = pd["top_programs"][0]["fitness"]
    terrain_inst = td.get("instances", ["terrain"])[0] if td.get("instances") else "terrain"

    print(f"[render] Simulating {N}×{N} grid...")
    data = run_sim(t_code, r_code, N=N)

    fig, axes = plt.subplots(1, 4, figsize=(18, 5))
    fig.patch.set_facecolor("#0a0a0f")

    terrain_arr = data["TERRAIN"]
    temp_arr    = data["TEMPERATURE"]
    phi_init    = data["PHI_INIT"]
    phi_final   = data["PHI_FINAL"]

    # Terrain heightmap with hillshade
    ax = axes[0]
    im = ax.imshow(terrain_arr, cmap="terrain", origin="lower", vmin=0, vmax=1)
    plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    ax.set_title(f"Terrain: {terrain_inst}\nfitness={terrain_fit:.4f}", color="white", fontsize=11)
    ax.set_xlabel("x", color="gray"); ax.set_ylabel("y", color="gray")

    # Temperature field (cold=blue, hot=red)
    ax = axes[1]
    im = ax.imshow(temp_arr, cmap="RdBu_r", origin="lower", vmin=0, vmax=1)
    plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    ax.set_title("Temperature field\n(blue=cold spell, red=warm)", color="white", fontsize=11)

    # Initial water distribution
    ax = axes[2]
    wet = phi_init > 0.01
    water_vis = np.zeros((*terrain_arr.shape, 4))
    water_vis[~wet] = [0.35, 0.25, 0.15, 1.0]   # dry land: brown
    water_vis[wet]  = [0.1,  0.4,  0.9,  1.0]    # water: blue
    ax.imshow(water_vis, origin="lower")
    ax.set_title("Initial state\n(blue=water, brown=dry land)", color="white", fontsize=11)

    # Final state: ice vs water vs dry
    ax = axes[3]
    final_vis = np.zeros((*phi_final.shape, 4))
    dry = ~wet
    liquid = wet & (phi_final < 0.5)
    ice    = wet & (phi_final >= 0.5)
    final_vis[dry]    = [0.35, 0.25, 0.15, 1.0]  # brown
    final_vis[liquid] = [0.1,  0.4,  0.9,  1.0]  # blue water
    final_vis[ice]    = [0.85, 0.92, 1.0,  1.0]  # white/light-blue ice
    ax.imshow(final_vis, origin="lower")
    ice_frac = ice.sum() / wet.sum() if wet.sum() > 0 else 0
    ax.set_title(f"After spell\n{ice_frac:.0%} of water frozen  (phase fit={phase_fit:.4f})",
                 color="white", fontsize=11)

    for ax in axes:
        ax.tick_params(colors="gray")
        for spine in ax.spines.values():
            spine.set_edgecolor("#333")

    fig.suptitle(
        "FunSearch Composition Oracle — TEngine Physics Readiness\n"
        f"Terrain: {terrain_inst} ({terrain_fit:.4f})  ·  Phase: Allen-Cahn reaction ({phase_fit:.4f})",
        color="white", fontsize=13, fontweight="bold", y=1.02
    )
    plt.tight_layout()
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"[render] Saved: {output}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("terrain_json")
    parser.add_argument("phase_json")
    parser.add_argument("--output", default="benchmarks/luts/composition_showcase.png")
    parser.add_argument("--size", type=int, default=64)
    args = parser.parse_args()
    render(args.terrain_json, args.phase_json, args.output, N=args.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
