#!/usr/bin/env python3
"""Render every evolved FunSearch kernel as publication-quality PNG visualizations.

Produces:
  benchmarks/renders/2026-05-31/terrain_{biome}.png  (5 files, 800×800)
  benchmarks/renders/2026-05-31/terrain_showcase.png  (2×3 composite)
  benchmarks/renders/2026-05-31/phase_evolution.png   (3-panel Allen-Cahn)
  benchmarks/renders/2026-05-31/sph_kernel_curve.png  (evolved vs Wendland C2)

Usage:
    python3 tools/render_evolved_kernels.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.gridspec import GridSpec
from scipy.ndimage import laplace as scipy_laplace

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "benchmarks" / "renders" / "2026-05-31"

# ---------------------------------------------------------------------------
# numpy vec2 helper: components are full-grid arrays
# ---------------------------------------------------------------------------

class V2:
    """Pair of same-shape numpy arrays behaving like a vec2."""
    __slots__ = ("x", "y")

    def __init__(self, x, y):
        self.x = np.asarray(x, dtype=np.float64)
        self.y = np.asarray(y, dtype=np.float64)

    def __add__(self, other):
        if isinstance(other, V2):
            return V2(self.x + other.x, self.y + other.y)
        return V2(self.x + other, self.y + other)

    def __radd__(self, other):
        return V2(self.x + other, self.y + other)

    def __sub__(self, other):
        if isinstance(other, V2):
            return V2(self.x - other.x, self.y - other.y)
        return V2(self.x - other, self.y - other)

    def __rsub__(self, other):
        return V2(other - self.x, other - self.y)

    def __mul__(self, other):
        if isinstance(other, V2):
            return V2(self.x * other.x, self.y * other.y)
        return V2(self.x * other, self.y * other)

    def __rmul__(self, other):
        return V2(self.x * other, self.y * other)

    def __truediv__(self, other):
        return V2(self.x / other, self.y / other)


def _fract(x):
    return x - np.floor(x)

def _mix(a, b, t):
    return a + t * (b - a)


# ---------------------------------------------------------------------------
# eroded_badlands  (fitness=0.9807)
# Uses smoothstep fade, 57-lattice hash
# ---------------------------------------------------------------------------

def _eb_th(n):
    return _fract(np.sin(n) * 43758.5453)

def _eb_tv(p: V2):
    """value noise — smoothstep fade, 57-lattice hash"""
    ix = np.floor(p.x); iy = np.floor(p.y)
    fx = _fract(p.x);   fy = _fract(p.y)
    fx = fx * fx * (3.0 - 2.0 * fx)
    fy = fy * fy * (3.0 - 2.0 * fy)
    n00 = _eb_th(ix       + iy       * 57.0)
    n10 = _eb_th(ix + 1.0 + iy       * 57.0)
    n01 = _eb_th(ix       + (iy+1.0) * 57.0)
    n11 = _eb_th(ix + 1.0 + (iy+1.0) * 57.0)
    return _mix(_mix(n00, n10, fx), _mix(n01, n11, fx), fy)

def terrain_eroded_badlands(p: V2) -> np.ndarray:
    v = np.zeros_like(p.x)
    a = 0.55
    q = V2(p.x * 1.3, p.y * 1.3)
    w = 0.5
    for i in range(8):
        warp_x = _eb_tv(q) * w
        warp_y = _eb_tv(V2(q.x + 31.7, q.y + 17.3)) * w
        n = _eb_tv(V2(p.x + warp_x, p.y + warp_y))
        if i == 0:
            v += a * np.abs(2.0 * n - 1.0)
        else:
            v += a * n
        p = V2(p.x * 2.0, p.y * 2.0)
        q = V2(q.x * 2.0, q.y * 2.0)
        a *= 0.63
    return v


# ---------------------------------------------------------------------------
# river_valley  (fitness=0.9997)
# Uses smoothstep fade, 57-lattice hash (same body as badlands hash,
# but no domain-warp — octave offsets instead)
# ---------------------------------------------------------------------------

def _rv_hash(n):
    return _fract(np.sin(n) * 43758.5453)

def _rv_vnoise(p: V2):
    ix = np.floor(p.x); iy = np.floor(p.y)
    fx = _fract(p.x);   fy = _fract(p.y)
    fx = fx * fx * (3.0 - 2.0 * fx)
    fy = fy * fy * (3.0 - 2.0 * fy)
    n00 = _rv_hash(ix       + iy       * 57.0)
    n10 = _rv_hash(ix + 1.0 + iy       * 57.0)
    n01 = _rv_hash(ix       + (iy+1.0) * 57.0)
    n11 = _rv_hash(ix + 1.0 + (iy+1.0) * 57.0)
    return _mix(_mix(n00, n10, fx), _mix(n01, n11, fx), fy)

def terrain_river_valley(p: V2) -> np.ndarray:
    v = np.zeros_like(p.x)
    a = 0.5
    for i in range(6):
        q = V2(p.x + 3.7 * (i + 1), p.y + 2.3 * (i + 1))
        v += a * _rv_vnoise(q)
        p = V2(p.x * 2.05, p.y * 2.05)
        a *= 0.48
    return v


# ---------------------------------------------------------------------------
# volcanic_plateau  (fitness=0.9876)
# Uses smoothstep fade, 57-lattice hash; ridged + domain-warp + rotation
# ---------------------------------------------------------------------------

def _vh_hash(n):
    return _fract(np.sin(n) * 43758.5453)

def _vh_vnoise(p: V2):
    ix = np.floor(p.x); iy = np.floor(p.y)
    fx = _fract(p.x);   fy = _fract(p.y)
    fx = fx * fx * (3.0 - 2.0 * fx)
    fy = fy * fy * (3.0 - 2.0 * fy)
    n00 = _vh_hash(ix       + iy       * 57.0)
    n10 = _vh_hash(ix + 1.0 + iy       * 57.0)
    n01 = _vh_hash(ix       + (iy+1.0) * 57.0)
    n11 = _vh_hash(ix + 1.0 + (iy+1.0) * 57.0)
    return _mix(_mix(n00, n10, fx), _mix(n01, n11, fx), fy)

def _vh_ridged(p: V2):
    n = _vh_vnoise(p)
    return 1.0 - np.abs(2.0 * n - 1.0)

def _vh_warp(p: V2, s: float):
    q = V2(_vh_vnoise(p), _vh_vnoise(V2(p.x + 5.2, p.y + 1.3)))
    return _vh_vnoise(V2(p.x + s * q.x, p.y + s * q.y))

def terrain_volcanic_plateau(p: V2) -> np.ndarray:
    v = np.zeros_like(p.x)
    a = 0.6
    for i in range(6):
        v += a * _vh_ridged(p)
        # rotation by ~28 degrees
        px_new = p.x * 0.883 - p.y * 0.469
        py_new = p.x * 0.469 + p.y * 0.883
        p = V2(px_new * 2.1, py_new * 2.1)
        a *= 0.58

    warp_p = V2(p.x * 0.6, p.y * 0.6)
    w = 0.3 * _vh_warp(warp_p, 1.2)

    s = np.zeros_like(p.x)
    sa = 0.4
    for i in range(4):
        s += sa * _vh_vnoise(p)
        p = V2(p.x * 2.0 + 1.7, p.y * 2.0 + 0.9)
        sa *= 0.5

    return v * 0.75 + w * 0.12 + s * 0.13


# ---------------------------------------------------------------------------
# rolling_hills  (fitness=0.9994)
# DIFFERENT: quintic fade f*f*f*(10-15f+6f*f) — NOT smoothstep
# ---------------------------------------------------------------------------

def _rh_hash(n):
    return _fract(np.sin(n) * 43758.5453)

def _rh_vnoise(p: V2):
    """value noise with QUINTIC fade (different from river/badlands)"""
    ix = np.floor(p.x); iy = np.floor(p.y)
    fx = _fract(p.x);   fy = _fract(p.y)
    # quintic: f*f*f*(10 - 15*f + 6*f*f)
    fx = fx * fx * fx * (10.0 - 15.0 * fx + 6.0 * fx * fx)
    fy = fy * fy * fy * (10.0 - 15.0 * fy + 6.0 * fy * fy)
    n00 = _rh_hash(ix       + iy       * 57.0)
    n10 = _rh_hash(ix + 1.0 + iy       * 57.0)
    n01 = _rh_hash(ix       + (iy+1.0) * 57.0)
    n11 = _rh_hash(ix + 1.0 + (iy+1.0) * 57.0)
    return _mix(_mix(n00, n10, fx), _mix(n01, n11, fx), fy)

def terrain_rolling_hills(p: V2) -> np.ndarray:
    v = np.zeros_like(p.x)
    a = 0.58
    for i in range(5):
        v += a * _rh_vnoise(p)
        # shearing transform (not a pure rotation — elongates hills)
        px_new = p.x * 1.78 + p.y * 0.35
        py_new = p.x * 0.35 + p.y * 1.78
        p = V2(px_new, py_new)
        a *= 0.53
    return v


# ---------------------------------------------------------------------------
# mountain_peaks  (fitness=0.9998)
# DIFFERENT: dot-product hash (127.1/311.7 lattice), ridged, 9 octaves
# ---------------------------------------------------------------------------

def _mp_hash(p: V2):
    """dot-product hash — completely different from 57-lattice"""
    vx = p.x * 127.1 + p.y * 311.7
    vy = p.x * 269.5 + p.y * 183.3
    return _fract(np.sin(vx + vy) * 43758.5453)

def _mp_perlin(p: V2):
    ix = np.floor(p.x); iy = np.floor(p.y)
    fx = _fract(p.x);   fy = _fract(p.y)
    fx = fx * fx * (3.0 - 2.0 * fx)
    fy = fy * fy * (3.0 - 2.0 * fy)
    a = _mp_hash(V2(ix,       iy      ))
    b = _mp_hash(V2(ix + 1.0, iy      ))
    c = _mp_hash(V2(ix,       iy + 1.0))
    d = _mp_hash(V2(ix + 1.0, iy + 1.0))
    return _mix(_mix(a, b, fx), _mix(c, d, fx), fy)

def terrain_mountain_peaks(p: V2) -> np.ndarray:
    # domain warp
    w1 = V2(
        _mp_perlin(V2(p.x * 1.2 + 1.7, p.y * 1.2 + 9.2)),
        _mp_perlin(V2(p.x * 1.5 + 8.3, p.y * 1.5 + 2.8)),
    )
    p = V2(p.x + w1.x * 0.31, p.y + w1.y * 0.31)

    v = np.zeros_like(p.x)
    a = 0.55
    w = np.ones_like(p.x)
    ox, oy = 3.9, 6.3
    for i in range(9):
        n = 1.0 - np.abs(2.0 * _mp_perlin(p) - 1.0)
        n = np.power(np.clip(n, 0, 1), 1.95)
        v += a * n * w
        w = np.clip(n * 1.28, 0.35, 1.0)
        p = V2(p.x * 2.03 + ox + w1.x * 0.07,
               p.y * 2.03 + oy + w1.y * 0.07)
        a *= 0.47
    return v


# ---------------------------------------------------------------------------
# Hillshading utility
# ---------------------------------------------------------------------------

def hillshade(h: np.ndarray, light: tuple = (1.0, 1.0, 2.0)) -> np.ndarray:
    """Lambert hillshading from gradient."""
    dy, dx = np.gradient(h)
    lx, ly, lz = light
    ln = np.sqrt(lx**2 + ly**2 + lz**2)
    lx, ly, lz = lx / ln, ly / ln, lz / ln
    # surface normal: (-dx, -dy, 1) normalized
    nx, ny, nz = -dx, -dy, np.ones_like(h)
    nmag = np.sqrt(nx**2 + ny**2 + nz**2)
    nx, ny, nz = nx / nmag, ny / nmag, nz / nmag
    shade = np.clip(nx * lx + ny * ly + nz * lz, 0, 1)
    # blend: 70% diffuse + 30% ambient
    return 0.7 * shade + 0.3


def blend_hillshade(h: np.ndarray, cmap_name: str) -> np.ndarray:
    """Return RGBA array: colormap blended with hillshade."""
    h_norm = (h - h.min()) / ((h.max() - h.min()) + 1e-9)
    shade = hillshade(h_norm)
    cm = plt.get_cmap(cmap_name)
    rgb = cm(h_norm)[..., :3]
    # multiply color by shade
    lit = rgb * shade[..., np.newaxis]
    lit = np.clip(lit, 0, 1)
    return lit


# ---------------------------------------------------------------------------
# Grid factory
# ---------------------------------------------------------------------------

def make_grid(N: int = 512, lo: float = -3.0, hi: float = 3.0) -> V2:
    xs = np.linspace(lo, hi, N)
    ys = np.linspace(lo, hi, N)
    xx, yy = np.meshgrid(xs, ys)
    return V2(xx, yy)


# ---------------------------------------------------------------------------
# Render individual terrain PNG
# ---------------------------------------------------------------------------

BIOME_META = {
    "eroded_badlands":  {"fn": terrain_eroded_badlands,  "cmap": "Oranges",    "fitness": 0.9807},
    "river_valley":     {"fn": terrain_river_valley,     "cmap": "RdYlBu_r",   "fitness": 0.9997},
    "volcanic_plateau": {"fn": terrain_volcanic_plateau, "cmap": "copper",     "fitness": 0.9876},
    "rolling_hills":    {"fn": terrain_rolling_hills,    "cmap": "YlGn",       "fitness": 0.9994},
    "mountain_peaks":   {"fn": terrain_mountain_peaks,   "cmap": "bone",       "fitness": 0.9998},
}

BIOME_ORDER = [
    "eroded_badlands",
    "river_valley",
    "volcanic_plateau",
    "rolling_hills",
    "mountain_peaks",
]


def render_terrain_single(biome_name: str, out_path: Path) -> np.ndarray:
    meta = BIOME_META[biome_name]
    print(f"  sampling {biome_name} (512×512)...")
    p = make_grid(512)
    h = meta["fn"](p)
    lit = blend_hillshade(h, meta["cmap"])

    dpi = 100
    fig, ax = plt.subplots(figsize=(8, 8), dpi=dpi)
    fig.patch.set_facecolor("#0a0a0e")
    ax.set_facecolor("#0a0a0e")

    ax.imshow(lit, origin="lower", extent=[-3, 3, -3, 3])

    # colorbar overlay (height)
    h_norm = (h - h.min()) / ((h.max() - h.min()) + 1e-9)
    im_cb = ax.imshow(h_norm, cmap=meta["cmap"], origin="lower",
                      extent=[-3, 3, -3, 3], alpha=0.0)
    cbar = fig.colorbar(im_cb, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label("normalised elevation", color="white", fontsize=10)
    cbar.ax.yaxis.set_tick_params(color="gray")
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color="gray")

    display_name = biome_name.replace("_", " ").title()
    ax.set_title(
        f"{display_name}\nFunSearch fitness = {meta['fitness']:.4f}",
        color="white", fontsize=14, fontweight="bold", pad=12,
    )
    ax.set_xlabel("x", color="gray", fontsize=11)
    ax.set_ylabel("y", color="gray", fontsize=11)
    ax.tick_params(colors="gray")
    for spine in ax.spines.values():
        spine.set_edgecolor("#444")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  saved: {out_path}")
    return h


# ---------------------------------------------------------------------------
# Terrain showcase composite (2×3 grid)
# ---------------------------------------------------------------------------

def render_terrain_showcase(heightmaps: dict[str, np.ndarray], out_path: Path):
    print("  compositing terrain_showcase.png...")

    fig = plt.figure(figsize=(18, 12), dpi=100)
    fig.patch.set_facecolor("#0a0a0e")

    gs = GridSpec(2, 3, figure=fig, hspace=0.40, wspace=0.25,
                  left=0.05, right=0.95, top=0.88, bottom=0.05)

    positions = [
        (0, 0, "eroded_badlands"),
        (0, 1, "river_valley"),
        (0, 2, "volcanic_plateau"),
        (1, 0, "rolling_hills"),
        (1, 1, "mountain_peaks"),
        (1, 2, None),  # summary panel
    ]

    for row, col, bname in positions:
        ax = fig.add_subplot(gs[row, col])
        ax.set_facecolor("#111118")

        if bname is None:
            # summary text panel
            lines = [
                "FunSearch Evolution",
                "Kernel Gallery",
                "",
                "eroded_badlands   0.9807",
                "river_valley          0.9997",
                "volcanic_plateau  0.9876",
                "rolling_hills         0.9994",
                "mountain_peaks    0.9998",
                "",
                "phase kernel (Allen-Cahn)  1.0000",
                "SPH smoothing kernel       0.9455",
                "",
                "Rendered 2026-05-31",
            ]
            ax.text(0.5, 0.5, "\n".join(lines),
                    transform=ax.transAxes,
                    ha="center", va="center",
                    fontsize=11, color="#d0d0e0",
                    fontfamily="monospace",
                    linespacing=1.7,
                    bbox=dict(boxstyle="round,pad=0.6", facecolor="#1a1a2e",
                              edgecolor="#4444aa", linewidth=1.5))
            ax.set_title("Summary", color="white", fontsize=12, fontweight="bold")
            ax.axis("off")
            continue

        meta = BIOME_META[bname]
        h = heightmaps[bname]
        lit = blend_hillshade(h, meta["cmap"])
        ax.imshow(lit, origin="lower")

        h_norm = (h - h.min()) / ((h.max() - h.min()) + 1e-9)
        im_cb = ax.imshow(h_norm, cmap=meta["cmap"], origin="lower", alpha=0.0)
        cb = fig.colorbar(im_cb, ax=ax, fraction=0.046, pad=0.04)
        cb.ax.tick_params(colors="gray", labelsize=7)

        display = bname.replace("_", " ").title()
        ax.set_title(f"{display}\nfit={meta['fitness']:.4f}",
                     color="white", fontsize=10, fontweight="bold")
        ax.set_xticks([]); ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_edgecolor("#333")

    fig.suptitle(
        "FunSearch Evolved Terrain Kernels — Complete Gallery",
        color="white", fontsize=16, fontweight="bold", y=0.94,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=100, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  saved: {out_path}")


# ---------------------------------------------------------------------------
# Allen-Cahn phase evolution
# ---------------------------------------------------------------------------

def reaction_vectorized(phi: np.ndarray, temp: float) -> np.ndarray:
    """Vectorized Allen-Cahn reaction term (exact translation of evolved C)."""
    dW = phi * phi * (4.0 * phi - 6.0) + 2.0 * phi
    m = 2.0 * (0.5 - temp)
    mobility = 1.0 + 0.6 * np.tanh(2.5 * (0.5 - temp))
    return -dW + m * mobility + 4.0 * phi * (1.0 - phi) * m


def simulate_allen_cahn(phi0: np.ndarray, temp: float, steps: int = 300,
                        D: float = 1.0, dt: float = 0.01) -> np.ndarray:
    phi = phi0.copy()
    for _ in range(steps):
        lap = scipy_laplace(phi, mode="reflect")
        r = reaction_vectorized(phi, temp)
        phi = phi + dt * (D * lap + r)
        phi = np.clip(phi, 0.0, 1.0)
    return phi


def render_phase_evolution(out_path: Path):
    print("  simulating Allen-Cahn phase evolution (3 scenarios)...")

    H, W = 128, 64  # task spec: 128×64 domain

    # scenario 1: freezing — cold, liquid starts near 0.1
    phi_freeze = np.full((H, W), 0.1) + np.random.RandomState(42).normal(0, 0.02, (H, W))
    phi_freeze = np.clip(phi_freeze, 0, 1)

    # scenario 2: melting — warm, solid starts near 0.9
    phi_melt = np.full((H, W), 0.9) + np.random.RandomState(43).normal(0, 0.02, (H, W))
    phi_melt = np.clip(phi_melt, 0, 1)

    # scenario 3: interface — phi has a sharp step at mid-height, temp=0.5 critical
    phi_iface = np.zeros((H, W))
    phi_iface[:H//2, :] = 1.0
    # add some perturbation at the interface
    rng = np.random.RandomState(44)
    noise = rng.normal(0, 0.05, (H, W))
    # smooth the interface over ~5 pixels
    from scipy.ndimage import gaussian_filter
    phi_iface = np.clip(phi_iface + noise, 0, 1)
    phi_iface = gaussian_filter(phi_iface.astype(float), sigma=2)
    phi_iface = np.clip(phi_iface, 0, 1)

    print("    simulating freeze (temp=0.2, 300 steps)...")
    phi_f_final = simulate_allen_cahn(phi_freeze, temp=0.2, steps=300)
    print("    simulating melt  (temp=0.8, 300 steps)...")
    phi_m_final = simulate_allen_cahn(phi_melt,   temp=0.8, steps=300)
    print("    simulating interface (temp=0.5, 300 steps)...")
    phi_i_final = simulate_allen_cahn(phi_iface,  temp=0.5, steps=300)

    fig, axes = plt.subplots(1, 3, figsize=(15, 6), dpi=100)
    fig.patch.set_facecolor("#0a0a0e")

    scenarios = [
        (phi_f_final, "Freezing\ntemp=0.2 (cold), φ₀≈0.1", "Blues_r"),
        (phi_m_final, "Melting\ntemp=0.8 (warm), φ₀≈0.9",  "YlOrRd"),
        (phi_i_final, "Interface\ntemp=0.5 (critical)",     "RdYlBu"),
    ]

    for ax, (phi, title, cmap) in zip(axes, scenarios):
        ax.set_facecolor("#0a0a0e")
        im = ax.imshow(phi, cmap=cmap, origin="lower", vmin=0, vmax=1,
                       aspect="auto")
        cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        cb.set_label("φ (order parameter)", color="white", fontsize=9)
        cb.ax.tick_params(colors="gray")
        plt.setp(cb.ax.yaxis.get_ticklabels(), color="gray")
        ax.set_title(title, color="white", fontsize=12, fontweight="bold")
        ax.set_xlabel("x", color="gray"); ax.set_ylabel("y", color="gray")
        ax.tick_params(colors="gray")
        for spine in ax.spines.values():
            spine.set_edgecolor("#444")

    fig.suptitle(
        "Allen-Cahn Phase Evolution — Evolved Reaction Kernel (fitness=1.0000)\n"
        "∂φ/∂t = D·∇²φ + reaction(φ, temp)  |  D=1.0, dt=0.01, 300 steps",
        color="white", fontsize=13, fontweight="bold", y=1.02,
    )
    plt.tight_layout()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=100, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  saved: {out_path}")


# ---------------------------------------------------------------------------
# SPH kernel curve
# ---------------------------------------------------------------------------

def evolved_sph(r: np.ndarray, h: float = 1.0) -> np.ndarray:
    """Evolved SPH kernel (exact translation of FunSearch output)."""
    q = r / h
    result = np.zeros_like(r)
    mask = q < 1.0
    q_m = q[mask]
    m = 1.0 - q_m
    sigma = 1536.0 / (478.0 * np.pi * h * h * h)
    poly = (1.0 + 5.0*q_m + 10.0*q_m**2 + 10.0*q_m**3
            + 5.0*q_m**4 + q_m**5)
    m5 = m * m; m5 = m5 * m5 * m  # (1-q)^5
    result[mask] = sigma * m5 * poly
    return result


def wendland_c2(r: np.ndarray, h: float = 1.0) -> np.ndarray:
    """Reference Wendland C2 kernel: sigma*(1-q)^4*(1+4q)."""
    q = r / h
    result = np.zeros_like(r)
    mask = q < 1.0
    q_m = q[mask]
    sigma = 21.0 / (2.0 * np.pi)   # 2D normalisation (h=1)
    result[mask] = sigma * (1.0 - q_m)**4 * (1.0 + 4.0 * q_m)
    return result


def render_sph_kernel(out_path: Path):
    print("  plotting SPH kernel curves...")

    r = np.linspace(0.0, 1.05, 500)

    W_ev = evolved_sph(r, h=1.0)
    W_wl = wendland_c2(r, h=1.0)

    # normalise both to peak=1 for shape comparison
    W_ev_n = W_ev / (W_ev.max() + 1e-12)
    W_wl_n = W_wl / (W_wl.max() + 1e-12)

    fig, axes = plt.subplots(1, 2, figsize=(13, 5), dpi=100)
    fig.patch.set_facecolor("#0a0a0e")

    # left panel: raw values
    ax = axes[0]
    ax.set_facecolor("#0d0d18")
    ax.plot(r, W_ev, color="#ff7043", linewidth=2.5, label=f"Evolved (fit=0.9455)")
    ax.plot(r, W_wl, color="#42a5f5", linewidth=2.0, linestyle="--",
            label="Wendland C²  (reference)")
    ax.axvline(1.0, color="#555", linewidth=1, linestyle=":")
    ax.set_xlabel("r / h", color="white", fontsize=12)
    ax.set_ylabel("W(r, h=1)", color="white", fontsize=12)
    ax.set_title("SPH Kernels — Raw Values", color="white", fontsize=13, fontweight="bold")
    ax.legend(facecolor="#1a1a2e", edgecolor="#444", labelcolor="white", fontsize=11)
    ax.tick_params(colors="gray"); ax.set_facecolor("#0d0d18")
    ax.spines["bottom"].set_color("#444"); ax.spines["left"].set_color("#444")
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.set_xlim(0, 1.1); ax.set_ylim(bottom=0)
    ax.grid(color="#222", linewidth=0.5)

    # right panel: shape-normalised
    ax = axes[1]
    ax.set_facecolor("#0d0d18")
    ax.plot(r, W_ev_n, color="#ff7043", linewidth=2.5, label="Evolved (normalised)")
    ax.plot(r, W_wl_n, color="#42a5f5", linewidth=2.0, linestyle="--",
            label="Wendland C² (normalised)")
    ax.fill_between(r, W_ev_n, W_wl_n, alpha=0.15, color="#aaaaff",
                    label="shape difference")
    ax.axvline(1.0, color="#555", linewidth=1, linestyle=":")
    ax.set_xlabel("r / h", color="white", fontsize=12)
    ax.set_ylabel("W / W(0)", color="white", fontsize=12)
    ax.set_title("Shape Comparison (peak-normalised)", color="white",
                 fontsize=13, fontweight="bold")
    ax.legend(facecolor="#1a1a2e", edgecolor="#444", labelcolor="white", fontsize=11)
    ax.tick_params(colors="gray")
    ax.spines["bottom"].set_color("#444"); ax.spines["left"].set_color("#444")
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.set_xlim(0, 1.1); ax.set_ylim(0, 1.05)
    ax.grid(color="#222", linewidth=0.5)

    fig.suptitle(
        "FunSearch Evolved SPH Smoothing Kernel vs Wendland C²  (fitness=0.9455)",
        color="white", fontsize=14, fontweight="bold", y=1.02,
    )
    plt.tight_layout()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=100, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  saved: {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {OUT_DIR}")

    # --- Terrain individual renders ---
    print("\n[1/4] Rendering individual terrain biomes...")
    heightmaps: dict[str, np.ndarray] = {}
    for biome in BIOME_ORDER:
        out = OUT_DIR / f"terrain_{biome}.png"
        h = render_terrain_single(biome, out)
        heightmaps[biome] = h

    # --- Terrain showcase ---
    print("\n[2/4] Rendering terrain showcase composite...")
    render_terrain_showcase(heightmaps, OUT_DIR / "terrain_showcase.png")

    # --- Phase evolution ---
    print("\n[3/4] Rendering Allen-Cahn phase evolution...")
    render_phase_evolution(OUT_DIR / "phase_evolution.png")

    # --- SPH kernel ---
    print("\n[4/4] Rendering SPH kernel curve...")
    render_sph_kernel(OUT_DIR / "sph_kernel_curve.png")

    # --- Summary ---
    print("\nDone. Generated files:")
    for p in sorted(OUT_DIR.glob("*.png")):
        size_kb = p.stat().st_size // 1024
        print(f"  {p}  ({size_kb} kB)")


if __name__ == "__main__":
    main()
