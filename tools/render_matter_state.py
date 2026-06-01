#!/usr/bin/env python3
"""Multi-angle, multi-timestep render of the full fluid/freeze matter pipeline.

Pipeline:
  1. Terrain (rolling_hills gen32, fit=0.999) → heightfield + valley mask
  2. SPH kernel (gen31, fit=0.9455)           → fluid density smoothing
  3. Phase kernel (gen16, fit=1.000)          → Allen-Cahn freeze (fixed T)
  4. Latent kernel (gen21, fit=0.650)         → Allen-Cahn freeze (coupled T)

Outputs (benchmarks/renders/matter_state_YYYYMMDD/):
  01_terrain_and_water.png          — terrain + water placement
  02_phase_freeze_strip.png         — 6-timestep freeze front (phase kernel)
  03_latent_freeze_strip.png        — 6-timestep freeze front (latent kernel, heat front visible)
  04_phase_vs_latent_final.png      — side-by-side final state comparison
  05_temperature_field.png          — temperature evolution during latent freeze
  06_cross_section.png              — XZ cross-section through freeze front
  07_isometric_composite.png        — fake isometric 3D view of final ice/water/land
  08_sph_smoothing.png              — SPH fluid density on terrain

Usage:
    python3 tools/render_matter_state.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import date

import numpy as np
from scipy.ndimage import laplace as scipy_laplace
from scipy.signal import fftconvolve
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.gridspec import GridSpec
from matplotlib.colors import LinearSegmentedColormap

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "benchmarks" / "renders" / f"matter_state_{date.today().strftime('%Y%m%d')}"

# ---------------------------------------------------------------------------
# Grid parameters
# ---------------------------------------------------------------------------
N = 128          # grid resolution — large enough for visual detail
# PDE parameters from autobench/latent_kernel/__init__.py
# _LATENT_INSTANCE_CONFIGS["freeze_latent"]: D_phi=10.0, D_T=5.0, L=0.4, dt=0.018
# Phase kernel (phase_kernel/__init__.py) uses a 1D PDE with D=50.0 and dt=0.006;
# DT_PHASE below is a 2D-visualization-only timestep (stability: D_PHI*dt <= 0.25).
DT_PHASE = 0.020   # TODO: phase_kernel uses 1D PDE — no authoritative 2D dt in result files
DT_LATENT = 0.018  # autobench/latent_kernel/__init__.py freeze_latent dt=0.018
D_PHI = 10.0       # autobench/latent_kernel/__init__.py freeze_latent D_phi=10.0
D_T = 5.0          # autobench/latent_kernel/__init__.py freeze_latent D_T=5.0
L_HEAT = 0.4       # autobench/latent_kernel/__init__.py freeze_latent L=0.4
WATER_LEVEL = 0.38   # terrain fraction below which is wet

# ---------------------------------------------------------------------------
# Colormaps
# ---------------------------------------------------------------------------

# Water/ice/land material colormap
_MATERIAL_COLORS = [
    (0.15, 0.40, 0.65),   # deep water (blue)
    (0.55, 0.80, 0.95),   # liquid near-shore
    (0.88, 0.95, 1.00),   # slush / thin ice
    (1.00, 1.00, 1.00),   # solid ice
    (0.52, 0.48, 0.42),   # bare rock / dry land
    (0.60, 0.72, 0.45),   # grass / highland
]
_MATERIAL_NODES = [0.0, 0.18, 0.35, 0.55, 0.70, 1.0]
MATERIAL_CMAP = LinearSegmentedColormap.from_list(
    "material",
    list(zip(_MATERIAL_NODES, _MATERIAL_COLORS)),
)

PHASE_CMAP = LinearSegmentedColormap.from_list(
    "phase_icewater",
    [(0.0, (0.10, 0.30, 0.70)),   # liquid (dark blue)
     (0.5, (0.65, 0.85, 0.98)),   # slush
     (1.0, (0.97, 0.99, 1.00))],  # ice (near white)
)

TEMP_CMAP = "RdYlBu_r"   # warm=red, cold=blue

# ---------------------------------------------------------------------------
# Rolling hills terrain kernel
# Source: benchmarks/curriculum/2026-05-31/terrain_results_gen32.json
#         top_programs[0].terrain_code  fitness=0.999395
# Literal numpy translation of the C loop — no coefficient substitution.
# ---------------------------------------------------------------------------

def _t_hash(n):
    return _fract(np.sin(n) * 43758.5453)

def _fract(x):
    return x - np.floor(x)

def _t_vnoise(x, y):
    """Value noise on a vec2 grid — quintic smootherstep, from gen32 terrain_code."""
    ix = np.floor(x).astype(float)
    iy = np.floor(y).astype(float)
    fx = x - ix
    fy = y - iy
    # Quintic smootherstep: f*f*f*(10-15*f+6*f*f)  — exact from source
    ux = fx * fx * fx * (10.0 - 15.0 * fx + 6.0 * fx * fx)
    uy = fy * fy * fy * (10.0 - 15.0 * fy + 6.0 * fy * fy)
    n00 = _t_hash(ix + iy * 57.0)
    n10 = _t_hash(ix + 1.0 + iy * 57.0)
    n01 = _t_hash(ix + (iy + 1.0) * 57.0)
    n11 = _t_hash(ix + 1.0 + (iy + 1.0) * 57.0)
    return n00 + ux * (n10 - n00) + uy * (n01 - n00) + ux * uy * (n11 - n10 - n01 + n00)

def terrain_rolling(px, py):
    """Rolling hills — 5-octave fBm loop from FunSearch gen32 (fitness=0.999395).

    Source: benchmarks/curriculum/2026-05-31/terrain_results_gen32.json
            top_programs[0].terrain_code

    C original:
        float v = 0.0f, a = 0.58f;
        for (int i = 0; i < 5; i++) {
            v += a * _t_vnoise(p);
            p  = vec2(p.x * 1.78f + p.y * 0.35f, p.x * 0.35f + p.y * 1.78f);
            a *= 0.53f;
        }
    Domain transform matrix [[1.78,0.35],[0.35,1.78]] applied at each iteration.
    Amplitudes: [0.58, 0.3074, 0.1629, 0.0863, 0.0458] — from a*=0.53 per step.
    """
    # Work on mutable copies so the domain transform iterates correctly
    qx = px.copy().astype(float)
    qy = py.copy().astype(float)
    v = np.zeros_like(qx)
    a = 0.58
    for _ in range(5):
        v += a * _t_vnoise(qx, qy)
        # Domain transform: p = [[1.78, 0.35],[0.35, 1.78]] * p
        qx_new = 1.78 * qx + 0.35 * qy
        qy_new = 0.35 * qx + 1.78 * qy
        qx, qy = qx_new, qy_new
        a *= 0.53
    return v

# ---------------------------------------------------------------------------
# Mountain peaks terrain
# Source: benchmarks/curriculum/2026-05-31/terrain_results_gen46.json
#         top_programs[0].terrain_code  fitness=0.999849
# Uses its own hash/perlin helpers (_h_hash, _h_perlin) — dot-product hash,
# cubic smoothstep — distinct from the rolling hills helpers above.
# ---------------------------------------------------------------------------

def _h_hash(px, py):
    """Dot-product hash from gen46 terrain_code.
    C: p = vec2(dot(p,vec2(127.1,311.7)), dot(p,vec2(269.5,183.3)));
       return fract(sinf(p.x+p.y)*43758.5453);
    """
    px2 = 127.1 * px + 311.7 * py
    py2 = 269.5 * px + 183.3 * py
    return _fract(np.sin(px2 + py2) * 43758.5453)

def _h_perlin(px, py):
    """Value noise with cubic smoothstep from gen46 terrain_code.
    C: vec2 i=floor(p), f=fract(p); f=f*f*(3-2*f);
       a=hash(i), b=hash(i+(1,0)), c=hash(i+(0,1)), d=hash(i+(1,1));
       return mix(mix(a,b,fx), mix(c,d,fx), fy)
    """
    ix = np.floor(px)
    iy = np.floor(py)
    fx = px - ix
    fy = py - iy
    # Cubic smoothstep: f*f*(3-2*f)
    ux = fx * fx * (3.0 - 2.0 * fx)
    uy = fy * fy * (3.0 - 2.0 * fy)
    a = _h_hash(ix,       iy      )
    b = _h_hash(ix + 1.0, iy      )
    c = _h_hash(ix,       iy + 1.0)
    d = _h_hash(ix + 1.0, iy + 1.0)
    ab = a + ux * (b - a)
    cd = c + ux * (d - c)
    return ab + uy * (cd - ab)

def terrain_mountain(px, py):
    """Mountain peaks — domain-warp + 9-octave ridged Perlin with erosion weight.

    Source: benchmarks/curriculum/2026-05-31/terrain_results_gen46.json
            top_programs[0].terrain_code  fitness=0.999849

    C original:
        vec2 w1 = vec2(_h_perlin(p*1.2+(1.7,9.2)), _h_perlin(p*1.5+(8.3,2.8)));
        p += w1 * 0.31;
        float v=0, a=0.55, w=1;
        const vec2 _o = (3.9,6.3);
        for (int i=0;i<9;i++) {
            float n = 1 - |2*_h_perlin(p)-1|;
            n = powf(n, 1.95);
            v += a * n * w;
            w = clamp(n*1.28, 0.35, 1.0);
            p = p*2.03 + _o + w1*0.07;
            a *= 0.47;
        }
    w is the erosion weight from the PREVIOUS iteration (starts at 1.0).
    """
    px = px.copy().astype(float)
    py = py.copy().astype(float)

    # Domain warp (computed once before the loop)
    w1x = _h_perlin(px * 1.2 + 1.7, py * 1.2 + 9.2)
    w1y = _h_perlin(px * 1.5 + 8.3, py * 1.5 + 2.8)
    px = px + w1x * 0.31
    py = py + w1y * 0.31

    _ox, _oy = 3.9, 6.3
    v = np.zeros_like(px)
    a = 0.55
    w = np.ones_like(px)   # erosion weight — starts at 1.0, updated after v+=

    for _ in range(9):
        n = 1.0 - np.abs(2.0 * _h_perlin(px, py) - 1.0)
        n = np.power(np.clip(n, 0.0, None), 1.95)
        v += a * n * w
        # Update erosion weight for next iteration
        w = np.clip(n * 1.28, 0.35, 1.0)
        # Advance domain: p = p*2.03 + _o + w1*0.07
        px_new = px * 2.03 + _ox + w1x * 0.07
        py_new = py * 2.03 + _oy + w1y * 0.07
        px, py = px_new, py_new
        a *= 0.47

    return v

# ---------------------------------------------------------------------------
# Build terrain + water grids
# ---------------------------------------------------------------------------

def build_terrain(terrain_fn=terrain_rolling, n=N):
    coords = np.linspace(-1.5, 1.5, n)
    px, py = np.meshgrid(coords, coords)
    H = terrain_fn(px, py)
    H = (H - H.min()) / (H.max() - H.min() + 1e-9)
    wet = H < WATER_LEVEL
    return H, wet

# ---------------------------------------------------------------------------
# SPH smoothing kernel
# Source: benchmarks/curriculum/2026-05-31/sph_results_gen31.json
#         top_programs[0].sph_code  fitness=0.9455389765150641
#
# C original:
#   float q = r / h;
#   if (q >= 1.0f) return 0.0f;
#   float m = 1.0f - q;
#   float sigma = 1536.0f / (478.0f * 3.14159265f * h * h * h);
#   float poly = 1 + 5q + 10q^2 + 10q^3 + 5q^4 + q^5;
#   float m5 = m*m; m5 *= m5*m;
#   return sigma * m5 * poly;
# ---------------------------------------------------------------------------

def sph_kernel(r, h):
    """Evolved SPH smoothing kernel W(r, h) — literal translation of gen31 sph_code.

    Source: benchmarks/curriculum/2026-05-31/sph_results_gen31.json
            top_programs[0].sph_code  fitness=0.9455389765150641
    """
    q = np.asarray(r, dtype=float) / h
    m = 1.0 - q
    sigma = 1536.0 / (478.0 * np.pi * h**3)
    poly = 1.0 + 5.0*q + 10.0*q**2 + 10.0*q**3 + 5.0*q**4 + q**5
    m5 = m * m
    m5 = m5 * m5 * m
    return np.where(q < 1.0, sigma * m5 * poly, 0.0)

def sph_density_field(wet_mask, n=N):
    """Compute SPH density field by convolving wet-mask particles with the evolved kernel.

    Uses fftconvolve for O(n^2 log n) instead of O(N_particles * n^2).
    Kernel evaluated at pixel-space radius so q=r/h_pix is dimensionless — the
    sigma normalisation constant is a global scale factor that imshow autoscales.
    """
    h_pix = max(4.0, n * 6.0 / 128.0)   # smoothing length in pixels; ~6px at N=128
    radius = int(np.ceil(h_pix))
    # Build radial kernel stencil at pixel resolution
    ks = 2 * radius + 1
    ki, kj = np.mgrid[-radius:radius+1, -radius:radius+1]
    kr = np.sqrt(ki**2 + kj**2)
    K = sph_kernel(kr, h_pix)
    K_sum = K.sum()
    if K_sum > 0:
        K = K / K_sum
    density = fftconvolve(wet_mask.astype(float), K, mode='same')
    return np.clip(density, 0.0, None)

# ---------------------------------------------------------------------------
# Phase kernel
# Source: benchmarks/curriculum/2026-05-31/phase_results_gen16.json
#         top_programs[0].reaction_code  fitness=1.0
#
# C original:
#   float dW = phi*phi*(4*phi-6)+2*phi;
#   float m  = 2*(0.5-temp);
#   float mobility = 1 + 0.6*tanhf(2.5*(0.5-temp));
#   return -dW + m*mobility + 4*phi*(1-phi)*m;
# ---------------------------------------------------------------------------

def phase_reaction(phi, temp):
    """Allen-Cahn driving force — literal translation of gen16 reaction_code.

    Source: benchmarks/curriculum/2026-05-31/phase_results_gen16.json
            top_programs[0].reaction_code  fitness=1.0
    """
    dW = phi * phi * (4.0 * phi - 6.0) + 2.0 * phi
    m = 2.0 * (0.5 - temp)
    mobility = 1.0 + 0.6 * np.tanh(2.5 * (0.5 - temp))
    return -dW + m * mobility + 4.0 * phi * (1.0 - phi) * m

# ---------------------------------------------------------------------------
# Latent kernel
# Source: benchmarks/curriculum/2026-05-31/latent_results_gen21.json
#         top_programs[0].reaction_code  fitness=0.650672
#
# C original:
#   float dW = phi*phi*(4*phi-6)+2*phi;
#   float m  = 2*(0.5-temp);
#   float iface = phi*(1-phi);
#   float amp = 1 + 4.5*iface;
#   float thermal_mod = 1 - 0.15*lap_T + 0.03*lap_T*lap_T;
#   float thermal_coupling = -0.7*iface*lap_T;
#   return (amp*(-dW+m))*fmaxf(0.05,thermal_mod) + thermal_coupling;
# ---------------------------------------------------------------------------

def latent_reaction(phi, temp, lap_T):
    """Coupled phase-thermal driving force — literal translation of gen21 reaction_code.

    Source: benchmarks/curriculum/2026-05-31/latent_results_gen21.json
            top_programs[0].reaction_code  fitness=0.650672
    """
    dW = phi * phi * (4.0 * phi - 6.0) + 2.0 * phi
    m = 2.0 * (0.5 - temp)
    iface = phi * (1.0 - phi)
    amp = 1.0 + 4.5 * iface
    thermal_mod = 1.0 - 0.15 * lap_T + 0.03 * lap_T * lap_T
    thermal_coupling = -0.7 * iface * lap_T
    return (amp * (-dW + m)) * np.maximum(0.05, thermal_mod) + thermal_coupling

# ---------------------------------------------------------------------------
# PDE steppers
# ---------------------------------------------------------------------------

def laplacian(field):
    return scipy_laplace(field, mode='nearest')

def step_phase(phi, T_field, wet_mask, dt=DT_PHASE):
    """One Allen-Cahn step with fixed temperature."""
    lap_phi = laplacian(phi)
    reaction = phase_reaction(phi, T_field)
    dphi = dt * (D_PHI * lap_phi + reaction)
    phi_new = np.clip(phi + dphi, 0.0, 1.0)
    phi_new[~wet_mask] = 0.0
    return phi_new

def step_latent(phi, T_field, wet_mask, dt=DT_LATENT):
    """One Allen-Cahn step with coupled temperature (latent heat feedback)."""
    lap_phi = laplacian(phi)
    lap_T   = laplacian(T_field)
    reaction = latent_reaction(phi, T_field, lap_T)
    dphi = dt * (D_PHI * lap_phi + reaction)
    phi_new = np.clip(phi + dphi, 0.0, 1.0)
    phi_new[~wet_mask] = 0.0
    # Temperature update: diffusion + latent heat source
    dT = dt * (D_T * lap_T + L_HEAT * dphi / dt)
    T_new = np.clip(T_field + dt * D_T * lap_T + L_HEAT * dphi, 0.0, 1.0)
    T_new[~wet_mask] = T_field[~wet_mask]   # dry land holds T
    return phi_new, T_new

# ---------------------------------------------------------------------------
# Initial conditions
# ---------------------------------------------------------------------------

def make_initial_conditions(wet_mask, n=N, cold_centre=True):
    """phi=0.05 (liquid) in wet cells. Cold spot in the centre."""
    phi = np.where(wet_mask, 0.05, 0.0)
    cx, cy = n // 2, n // 2
    ys, xs = np.ogrid[:n, :n]
    r = np.sqrt((ys - cy)**2 + (xs - cx)**2)
    # Cold zone: inner 30% radius; hot zone: outer
    T_field = np.where(r < n * 0.28, 0.22, 0.78)
    # Nucleation seed at coldest wet cells near centre
    seed_mask = (r < n * 0.08) & wet_mask
    phi[seed_mask] = 0.70
    return phi, T_field

# ---------------------------------------------------------------------------
# Simulation runners
# ---------------------------------------------------------------------------

FRAME_STEPS = 40    # PDE steps between render snapshots
N_FRAMES = 6

def run_phase_simulation(wet_mask):
    phi, T = make_initial_conditions(wet_mask)
    frames = [phi.copy()]
    for _ in range(N_FRAMES - 1):
        for _ in range(FRAME_STEPS):
            phi = step_phase(phi, T, wet_mask)
        frames.append(phi.copy())
    return frames, T

def run_latent_simulation(wet_mask):
    phi, T = make_initial_conditions(wet_mask)
    phi_frames, T_frames = [phi.copy()], [T.copy()]
    for _ in range(N_FRAMES - 1):
        for _ in range(FRAME_STEPS):
            phi, T = step_latent(phi, T, wet_mask)
        phi_frames.append(phi.copy())
        T_frames.append(T.copy())
    return phi_frames, T_frames

# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def render_material(H, phi, wet_mask, ax, title=""):
    """Material visualization: ice/water/dry land combined."""
    # Composite: wet cells blend between water (phi=0) and ice (phi=1)
    # Dry cells show terrain height as rock/grass
    img = np.zeros((*H.shape, 3))
    water_blue  = np.array([0.12, 0.35, 0.72])
    ice_white   = np.array([0.93, 0.97, 1.00])
    slush_cyan  = np.array([0.55, 0.85, 0.96])
    rock_brown  = np.array([0.52, 0.46, 0.38])
    grass_green = np.array([0.48, 0.65, 0.30])
    snow_white  = np.array([0.95, 0.96, 0.97])

    for c in range(3):
        # Wet cells: interpolate water → slush → ice by phi
        wet_color = np.where(
            phi < 0.4,
            water_blue[c] + phi / 0.4 * (slush_cyan[c] - water_blue[c]),
            slush_cyan[c] + (phi - 0.4) / 0.6 * (ice_white[c] - slush_cyan[c])
        )
        # Dry cells: interpolate rock → grass → snow by height
        H_dry = np.clip((H - WATER_LEVEL) / (1.0 - WATER_LEVEL), 0, 1)
        dry_color = np.where(
            H_dry < 0.5,
            rock_brown[c] + H_dry / 0.5 * (grass_green[c] - rock_brown[c]),
            grass_green[c] + (H_dry - 0.5) / 0.5 * (snow_white[c] - grass_green[c])
        )
        img[:, :, c] = np.where(wet_mask, wet_color, dry_color)

    img = np.clip(img, 0, 1)
    ax.imshow(img, origin='lower', interpolation='bilinear')
    ax.set_title(title, fontsize=9, fontweight='bold', pad=3)
    ax.axis('off')

def render_temperature(T, ax, title=""):
    m = ax.imshow(T, cmap=TEMP_CMAP, vmin=0.15, vmax=0.85,
                  origin='lower', interpolation='bilinear')
    ax.set_title(title, fontsize=9, pad=3)
    ax.axis('off')
    return m

def render_phase_field(phi, ax, title=""):
    ax.imshow(phi, cmap=PHASE_CMAP, vmin=0.0, vmax=1.0,
              origin='lower', interpolation='bilinear')
    ax.set_title(title, fontsize=9, pad=3)
    ax.axis('off')

# ---------------------------------------------------------------------------
# Isometric projection helper
# ---------------------------------------------------------------------------

def isometric_render(H, phi, wet_mask, out_path, n=N):
    """Fake isometric 3D: project heightfield with material coloring."""
    fig, ax = plt.subplots(figsize=(10, 7), facecolor='#0a0a14')
    ax.set_facecolor('#0a0a14')

    # Isometric transform: (i,j,h) → (x_screen, y_screen)
    # x_sc = (j - i) * cos30
    # y_sc = (j + i) * sin30 + h * height_scale
    cos30 = np.cos(np.radians(30))
    sin30 = np.sin(np.radians(30))
    height_scale = n * 0.35

    # Draw scanlines bottom-to-top (painter's algorithm)
    ys, xs = np.mgrid[:n, :n]
    # Sort cells by screen depth (i+j descending = back to front)
    order = np.argsort(-(ys + xs), axis=None)
    flat_i = ys.ravel()[order]
    flat_j = xs.ravel()[order]

    H_flat = H.ravel()[order]
    phi_flat = phi.ravel()[order]
    wet_flat = wet_mask.ravel()[order]

    cell_size = 1.0
    for k in range(len(flat_i)):
        i, j = flat_i[k], flat_j[k]
        h_val = H_flat[k]
        phi_val = phi_flat[k]
        is_wet = wet_flat[k]

        # Top face center in screen coords
        sx = (j - i) * cos30
        sy = (j + i) * sin30 + h_val * height_scale

        # Color by material
        if is_wet:
            if phi_val > 0.6:
                fc = (0.88, 0.96, 1.0, 1.0)    # ice
                ec = (0.7, 0.85, 0.95, 0.6)
            elif phi_val > 0.3:
                fc = (0.50, 0.78, 0.95, 0.95)  # slush
                ec = (0.4, 0.65, 0.85, 0.5)
            else:
                fc = (0.10, 0.28, 0.72, 0.9)   # water
                ec = (0.08, 0.22, 0.60, 0.5)
        else:
            t = np.clip((h_val - WATER_LEVEL) / (1.0 - WATER_LEVEL), 0, 1)
            if t > 0.75:
                fc = (0.93, 0.94, 0.97, 1.0)   # snow cap
            elif t > 0.4:
                fc = (0.45, 0.62, 0.28, 1.0)   # grass
            else:
                fc = (0.50, 0.44, 0.36, 1.0)   # rock

        # Draw small top-face square
        hw = 0.55
        corners_x = [sx - hw * cos30, sx, sx + hw * cos30, sx, sx - hw * cos30]
        corners_y = [sy, sy - hw * sin30, sy, sy + hw * sin30, sy]
        ax.fill(corners_x, corners_y, color=fc[:3], alpha=fc[3], linewidth=0)

    ax.set_aspect('equal')
    ax.axis('off')
    ax.set_title("Isometric view — terrain + ice/water/land (FunSearch kernels)",
                  color='white', fontsize=11, pad=8)
    plt.tight_layout(pad=0.5)
    fig.savefig(out_path, dpi=110, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  wrote {out_path.name}")

# ---------------------------------------------------------------------------
# Cross-section renderer
# ---------------------------------------------------------------------------

def render_cross_section(H, phi_phase, phi_latent, T_latent, out_path):
    """XZ cross-section through the centre row."""
    n = H.shape[0]
    mid = n // 2
    x = np.linspace(-1.5, 1.5, n)

    fig, axes = plt.subplots(3, 1, figsize=(12, 8), facecolor='#0d1117')
    fig.patch.set_facecolor('#0d1117')

    for ax in axes:
        ax.set_facecolor('#141920')
        for spine in ax.spines.values():
            spine.set_color('#334455')

    # Panel 1: terrain + water surface
    ax = axes[0]
    h_row = H[mid, :]
    wet_row = h_row < WATER_LEVEL
    ax.fill_between(x, 0, h_row, where=~wet_row, color='#7a6a54', alpha=0.9, label='Terrain')
    ax.fill_between(x, 0, h_row, where=wet_row, color='#1a4a8a', alpha=0.85, label='Water depth')
    ax.axhline(WATER_LEVEL, color='#4488cc', lw=1.2, ls='--', alpha=0.6, label='Water level')
    ax.set_ylabel('Height', color='#aabbcc', fontsize=9)
    ax.set_title('Cross-section: terrain + water (y=centre row)', color='white', fontsize=10)
    ax.legend(loc='upper right', fontsize=8, facecolor='#0d1117', labelcolor='white', framealpha=0.7)
    ax.tick_params(colors='#556677')

    # Panel 2: phase field comparison (final frame)
    ax = axes[1]
    phi_p_row = phi_phase[mid, :]
    phi_l_row = phi_latent[mid, :]
    ax.fill_between(x, 0, phi_p_row, where=wet_row, color='#3399ff', alpha=0.7, label='Phase kernel (fixed T)')
    ax.fill_between(x, 0, phi_l_row, where=wet_row, color='#cc4488', alpha=0.7, label='Latent kernel (coupled T)')
    ax.axhline(0.5, color='#ffee66', lw=0.8, ls=':', alpha=0.7, label='φ=0.5 (freeze threshold)')
    ax.set_ylim(0, 1.05)
    ax.set_ylabel('φ (solid fraction)', color='#aabbcc', fontsize=9)
    ax.set_title('Phase order parameter φ: fixed-T vs latent-coupled (final frame)', color='white', fontsize=10)
    ax.legend(loc='upper right', fontsize=8, facecolor='#0d1117', labelcolor='white', framealpha=0.7)
    ax.tick_params(colors='#556677')

    # Panel 3: temperature field (latent kernel)
    ax = axes[2]
    T_row = T_latent[mid, :]
    ax.plot(x, T_row, color='#ff6644', lw=1.8, label='Temperature T (latent run)')
    ax.axhline(0.5, color='#ffee66', lw=0.8, ls='--', alpha=0.7, label='Melting point T=0.5')
    ax.fill_between(x, 0, T_row, where=wet_row & (T_row < 0.5), color='#4488ff', alpha=0.3, label='Cold wet zone')
    ax.fill_between(x, T_row, 1.0, where=wet_row & (T_row > 0.5), color='#ff8844', alpha=0.2, label='Hot wet zone')
    ax.set_ylim(0, 1.05)
    ax.set_xlabel('x', color='#aabbcc', fontsize=9)
    ax.set_ylabel('Temperature', color='#aabbcc', fontsize=9)
    ax.set_title('Temperature field after latent-heat-coupled freeze (heat front visible)', color='white', fontsize=10)
    ax.legend(loc='upper right', fontsize=8, facecolor='#0d1117', labelcolor='white', framealpha=0.7)
    ax.tick_params(colors='#556677')

    plt.tight_layout(pad=1.2)
    fig.savefig(out_path, dpi=130, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  wrote {out_path.name}")

# ---------------------------------------------------------------------------
# Main render pipeline
# ---------------------------------------------------------------------------

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Output: {OUT_DIR}")

    print("Building terrain grid (N={})...".format(N))
    H, wet_mask = build_terrain(terrain_rolling, N)

    # ── 01: Terrain + water placement ───────────────────────────────────────
    print("Render 01: terrain + water...")
    fig, axes = plt.subplots(1, 3, figsize=(15, 5), facecolor='#0d1117')
    fig.patch.set_facecolor('#0d1117')

    # Heightfield
    ax = axes[0]
    ax.imshow(H, cmap='terrain', origin='lower', interpolation='bilinear')
    ax.set_title('Terrain heightfield\n(rolling_hills gen32, fit=0.999)', color='white', fontsize=9)
    ax.axis('off')

    # Water mask
    ax = axes[1]
    cmap_water = LinearSegmentedColormap.from_list('wm', [(0, (0.52, 0.46, 0.38)), (1, (0.12, 0.35, 0.72))])
    ax.imshow(wet_mask.astype(float), cmap=cmap_water, origin='lower', interpolation='nearest')
    ax.set_title(f'Water placement\n(terrain < {WATER_LEVEL:.0%} — valley fill)', color='white', fontsize=9)
    ax.axis('off')

    # SPH density
    ax = axes[2]
    sph_dens = sph_density_field(wet_mask, N)
    ax.imshow(sph_dens, cmap='Blues', origin='lower', interpolation='bilinear')
    ax.set_title('SPH density field\n(evolved kernel gen31, fit=0.946)', color='white', fontsize=9)
    ax.axis('off')

    for ax in axes:
        ax.set_facecolor('#0d1117')
    plt.tight_layout(pad=0.5)
    fig.savefig(OUT_DIR / "01_terrain_and_water.png", dpi=130, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print("  wrote 01_terrain_and_water.png")

    # ── Run simulations ──────────────────────────────────────────────────────
    print("Running phase simulation (fixed T)...")
    phase_frames, T_init = run_phase_simulation(wet_mask)

    print("Running latent simulation (coupled T)...")
    latent_phi_frames, latent_T_frames = run_latent_simulation(wet_mask)

    # ── 02: Phase freeze strip ───────────────────────────────────────────────
    print("Render 02: phase freeze strip...")
    fig, axes = plt.subplots(1, N_FRAMES, figsize=(18, 3.5), facecolor='#0d1117')
    fig.patch.set_facecolor('#0d1117')
    for k, phi in enumerate(phase_frames):
        step_label = f"t={k * FRAME_STEPS * DT_PHASE:.1f}"
        render_material(H, phi, wet_mask, axes[k], title=step_label)
        axes[k].set_facecolor('#0d1117')
    fig.suptitle("Phase kernel freeze (fixed T) — ice propagates from cold centre\n(Allen-Cahn gen16, fit=1.0, tanh mobility)",
                  color='white', fontsize=10, y=1.02)
    plt.tight_layout(pad=0.3)
    fig.savefig(OUT_DIR / "02_phase_freeze_strip.png", dpi=130, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print("  wrote 02_phase_freeze_strip.png")

    # ── 03: Latent freeze strip ──────────────────────────────────────────────
    print("Render 03: latent freeze strip...")
    fig, axes = plt.subplots(1, N_FRAMES, figsize=(18, 3.5), facecolor='#0d1117')
    fig.patch.set_facecolor('#0d1117')
    for k, phi in enumerate(latent_phi_frames):
        step_label = f"t={k * FRAME_STEPS * DT_LATENT:.1f}"
        render_material(H, phi, wet_mask, axes[k], title=step_label)
        axes[k].set_facecolor('#0d1117')
    fig.suptitle("Latent kernel freeze (coupled T) — heat-front limits propagation\n(Allen-Cahn gen21, fit=0.65, latent heat feedback)",
                  color='white', fontsize=10, y=1.02)
    plt.tight_layout(pad=0.3)
    fig.savefig(OUT_DIR / "03_latent_freeze_strip.png", dpi=130, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print("  wrote 03_latent_freeze_strip.png")

    # ── 04: Phase vs latent final comparison ────────────────────────────────
    print("Render 04: phase vs latent final comparison...")
    fig, axes = plt.subplots(2, 3, figsize=(15, 10), facecolor='#0d1117')
    fig.patch.set_facecolor('#0d1117')
    phi_p_final = phase_frames[-1]
    phi_l_final = latent_phi_frames[-1]
    T_l_final   = latent_T_frames[-1]

    render_material(H, phi_p_final, wet_mask, axes[0, 0], "Phase kernel: final ice state")
    render_material(H, phi_l_final, wet_mask, axes[0, 1], "Latent kernel: final ice state")
    axes[0, 0].set_facecolor('#0d1117')
    axes[0, 1].set_facecolor('#0d1117')

    # Diff: more ice = phase (fixed T freezes more aggressively)
    diff = phi_p_final - phi_l_final
    im = axes[0, 2].imshow(diff, cmap='RdBu', vmin=-0.6, vmax=0.6,
                           origin='lower', interpolation='bilinear')
    axes[0, 2].set_title("Δφ = Phase − Latent\n(red=more ice in fixed-T, blue=latent suppressed)",
                          color='white', fontsize=8)
    axes[0, 2].axis('off')
    axes[0, 2].set_facecolor('#0d1117')
    plt.colorbar(im, ax=axes[0, 2], shrink=0.8, label='φ difference')

    # Phase field raw
    render_phase_field(phi_p_final * wet_mask, axes[1, 0], "Phase order φ (fixed-T kernel)")
    render_phase_field(phi_l_final * wet_mask, axes[1, 1], "Phase order φ (latent-coupled)")
    axes[1, 0].set_facecolor('#0d1117')
    axes[1, 1].set_facecolor('#0d1117')
    m = render_temperature(T_l_final, axes[1, 2], "Temperature T after latent freeze\n(red=hot, blue=cold)")
    axes[1, 2].set_facecolor('#0d1117')
    plt.colorbar(m, ax=axes[1, 2], shrink=0.8, label='Temperature')

    fig.suptitle("Fixed-T vs Latent-Coupled Freeze: Final State Comparison",
                  color='white', fontsize=12, fontweight='bold', y=1.01)
    plt.tight_layout(pad=0.8)
    fig.savefig(OUT_DIR / "04_phase_vs_latent_final.png", dpi=130, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print("  wrote 04_phase_vs_latent_final.png")

    # ── 05: Temperature evolution ────────────────────────────────────────────
    print("Render 05: temperature evolution...")
    fig, axes = plt.subplots(2, 3, figsize=(15, 10), facecolor='#0d1117')
    fig.patch.set_facecolor('#0d1117')
    for k in range(N_FRAMES):
        ax = axes[k // 3, k % 3]
        ax.set_facecolor('#0d1117')
        T = latent_T_frames[k]
        phi = latent_phi_frames[k]
        # Overlay: temperature as background, phase contour
        m = ax.imshow(T, cmap=TEMP_CMAP, vmin=0.15, vmax=0.85,
                      origin='lower', interpolation='bilinear', alpha=0.85)
        # Phase front contour at phi=0.5
        ax.contour(phi * wet_mask, levels=[0.5], colors=['white'], linewidths=1.0, alpha=0.9)
        ax.contourf(~wet_mask, levels=[0.5, 1.5], colors=['#3a3028'], alpha=0.6)
        t_label = f"t={k * FRAME_STEPS * DT_LATENT:.1f}"
        ax.set_title(t_label, color='white', fontsize=9, pad=3)
        ax.axis('off')
    fig.suptitle("Temperature field evolution during latent-heat-coupled freeze\n(white contour = ice front φ=0.5, red=warm, blue=cold)",
                  color='white', fontsize=10, y=1.01)
    plt.tight_layout(pad=0.5)
    # Add colorbar
    cbar_ax = fig.add_axes([0.93, 0.15, 0.015, 0.7])
    plt.colorbar(m, cax=cbar_ax, label='Temperature')
    cbar_ax.tick_params(colors='white')
    cbar_ax.yaxis.label.set_color('white')
    fig.savefig(OUT_DIR / "05_temperature_field.png", dpi=130, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print("  wrote 05_temperature_field.png")

    # ── 06: Cross-section ────────────────────────────────────────────────────
    print("Render 06: cross-section...")
    render_cross_section(H, phi_p_final, phi_l_final, T_l_final,
                         OUT_DIR / "06_cross_section.png")

    # ── 07: Isometric composite ──────────────────────────────────────────────
    print("Render 07: isometric (this may take ~30s)...")
    # Sub-sample for isometric (it's O(n^2) draw calls)
    n_iso = 64
    H_small = H[::N//n_iso, ::N//n_iso][:n_iso, :n_iso]
    phi_small = phi_l_final[::N//n_iso, ::N//n_iso][:n_iso, :n_iso]
    wet_small = wet_mask[::N//n_iso, ::N//n_iso][:n_iso, :n_iso]
    H_small = (H_small - H_small.min()) / (H_small.max() - H_small.min() + 1e-9)
    isometric_render(H_small, phi_small, wet_small,
                     OUT_DIR / "07_isometric_composite.png", n=n_iso)

    # ── 08: SPH smoothing detail ──────────────────────────────────────────────
    print("Render 08: SPH smoothing detail...")
    fig, axes = plt.subplots(1, 3, figsize=(15, 5), facecolor='#0d1117')
    fig.patch.set_facecolor('#0d1117')

    # Raw particle mask
    ax = axes[0]
    ax.imshow(wet_mask.astype(float), cmap='Blues', origin='lower', interpolation='nearest')
    ax.set_title('Fluid particles (valley mask)', color='white', fontsize=9)
    ax.axis('off')

    # SPH smoothed density
    ax = axes[1]
    dens = sph_density_field(wet_mask, N)
    ax.imshow(dens, cmap='Blues', origin='lower', interpolation='bilinear')
    ax.set_title('SPH density W(r,h)\n(evolved kernel gen31, fit=0.946)', color='white', fontsize=9)
    ax.axis('off')

    # Kernel profile 1D plot
    ax = axes[2]
    ax.set_facecolor('#141920')
    r_vals = np.linspace(0, 1.05, 200)
    h_val = 1.0
    w_evolved = np.array([sph_kernel(r, h_val) for r in r_vals])
    # Wendland C2 reference
    q_ref = r_vals / h_val
    w_wendland = np.where(q_ref < 1, (1 - q_ref)**4 * (1 + 4*q_ref), 0.0)
    w_wendland /= (w_wendland.max() + 1e-9)
    w_evolved_norm = w_evolved / (w_evolved.max() + 1e-9)
    ax.plot(r_vals, w_evolved_norm, color='#3399ff', lw=2.2, label='Evolved SPH (gen31)')
    ax.plot(r_vals, w_wendland, color='#ff9944', lw=1.5, ls='--', label='Wendland C2 reference')
    ax.axvline(h_val, color='#88aa66', lw=0.8, ls=':', label='r = h (compact support)')
    ax.set_xlabel('r', color='#aabbcc')
    ax.set_ylabel('W(r,h) normalised', color='#aabbcc')
    ax.set_title('SPH kernel profile comparison', color='white', fontsize=9)
    ax.legend(fontsize=8, facecolor='#0d1117', labelcolor='white', framealpha=0.7)
    ax.tick_params(colors='#556677')
    for spine in ax.spines.values():
        spine.set_color('#334455')

    for ax in axes[:2]:
        ax.set_facecolor('#0d1117')
    plt.tight_layout(pad=0.5)
    fig.savefig(OUT_DIR / "08_sph_smoothing.png", dpi=130, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print("  wrote 08_sph_smoothing.png")

    # ── Summary mosaic ───────────────────────────────────────────────────────
    print("Render: summary mosaic...")
    from PIL import Image
    panels = [
        ("01_terrain_and_water.png", "Terrain + Water"),
        ("02_phase_freeze_strip.png", "Phase Freeze"),
        ("03_latent_freeze_strip.png", "Latent Freeze"),
        ("04_phase_vs_latent_final.png", "Final Comparison"),
        ("05_temperature_field.png", "Temperature Evolution"),
        ("06_cross_section.png", "Cross-Section"),
    ]
    imgs = [Image.open(OUT_DIR / p) for p, _ in panels]
    # Resize to consistent width
    W = 1200
    resized = [img.resize((W, int(img.height * W / img.width)), Image.LANCZOS) for img in imgs]
    total_h = sum(r.height for r in resized)
    mosaic = Image.new('RGB', (W, total_h), (13, 17, 23))
    y = 0
    for img in resized:
        mosaic.paste(img, (0, y))
        y += img.height
    mosaic.save(OUT_DIR / "00_mosaic.png")
    print("  wrote 00_mosaic.png")

    print(f"\nAll renders complete → {OUT_DIR}")
    return str(OUT_DIR)

if __name__ == "__main__":
    main()
