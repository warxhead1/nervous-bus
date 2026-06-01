#!/usr/bin/env python3
"""
tengine_bench_capture.py — Tile benchmark capture + replay for FunSearch ESS predicates.

Manages 64×64 tile .bench files: generate synthetic tiles, inspect headers,
and replay ESS predicates (should_skip) against captured ray/SVDAG data.

Live capture requires a new AgentCommand variant:
  DumpTile { x: u32, z: u32, output_path: PathBuf }
in ~/projects/tengine/crates/tengine-dgc-hal/src/silo/agent_commands.rs

The handler would:
1. Pause DGC lane for 1 frame
2. Read validated_terrain_addr (from LiveStreamState in live_stream.rs) to get
   the heightmap pointer
3. Read 64×64 block starting at tile (x, z)
4. Read SVDAG node data from svdag_sample.slang's node buffer
5. Write .bench file
6. Resume DGC lane

Until that AgentCommand is wired, use 'generate-synthetic' to create realistic
proxy tiles for FunSearch candidate scoring.

.bench format (version 1, 245,792 bytes total, little-endian):

  Header (32 bytes):
    magic:          4 bytes  = b"BNCH"
    version:        2 bytes  = 1
    tile_w:         2 bytes  = 64
    tile_h:         2 bytes  = 64
    ray_format:     2 bytes  = 1  (origin+dir, f32×3 each)
    height_format:  2 bytes  = 1  (f32 per pixel)
    svdag_format:   2 bytes  = 1  (stub nodes)
    reserved:      16 bytes  = 0

  Ray block (64×64 × 24 bytes = 98,304 bytes):
    per ray: origin_x, origin_y, origin_z, dir_x, dir_y, dir_z (all f32)
    stored row-major

  Heightmap block (64×64 × 4 bytes = 16,384 bytes):
    per pixel: terrain_height as f32 in [0, 1]

  SVDAG stub block (64×64 × 32 bytes = 131,072 bytes):
    per cell: node_min_sdf(f32), ray_dist_to_near(f32), depth(u32),
              node_extent(f32), child_mask(u32), parent_sdf(f32),
              reserved1(f32), reserved2(f32)
"""

import argparse
import struct
import sys
import time
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

MAGIC = b"BNCH"
VERSION = 1
TILE_W = 64
TILE_H = 64
N_RAYS = TILE_W * TILE_H  # 4096

HEADER_FMT = "<4sHHHHHH16s"  # 4 + 2+2+2+2+2+2 + 16 = 32 bytes
HEADER_SIZE = struct.calcsize(HEADER_FMT)
assert HEADER_SIZE == 32, f"Header is {HEADER_SIZE} bytes, expected 32"

RAY_FMT = "<ffffff"  # 6 × f32 = 24 bytes
RAY_SIZE = struct.calcsize(RAY_FMT)
assert RAY_SIZE == 24

HEIGHT_FMT = "<f"  # 1 × f32 = 4 bytes
HEIGHT_SIZE = struct.calcsize(HEIGHT_FMT)
assert HEIGHT_SIZE == 4

SVDAG_FMT = "<ffIfIfff"  # f32,f32,u32,f32,u32,f32,f32,f32 = 32 bytes
SVDAG_SIZE = struct.calcsize(SVDAG_FMT)
assert SVDAG_SIZE == 32, f"SVDAG cell is {SVDAG_SIZE} bytes, expected 32"

EXPECTED_TOTAL = HEADER_SIZE + N_RAYS * RAY_SIZE + N_RAYS * HEIGHT_SIZE + N_RAYS * SVDAG_SIZE
assert EXPECTED_TOTAL == 245_792, f"Expected 245792 bytes, got {EXPECTED_TOTAL}"


# ---------------------------------------------------------------------------
# FBM noise (pure numpy, no external deps)
# ---------------------------------------------------------------------------

def _fade(t: np.ndarray) -> np.ndarray:
    return t * t * t * (t * (t * 6 - 15) + 10)


def _lerp(a: np.ndarray, b: np.ndarray, t: np.ndarray) -> np.ndarray:
    return a + t * (b - a)


def _gradient_noise_2d(x: np.ndarray, y: np.ndarray, seed: int) -> np.ndarray:
    """Return Perlin-style gradient noise for arrays x, y."""
    rng = np.random.default_rng(seed)
    table_size = 256
    # Random gradients on unit circle
    angles = rng.uniform(0, 2 * np.pi, table_size)
    gx = np.cos(angles)
    gy = np.sin(angles)
    perm = rng.permutation(table_size).astype(np.int32)

    xi = np.floor(x).astype(np.int32)
    yi = np.floor(y).astype(np.int32)
    xf = x - np.floor(x)
    yf = y - np.floor(y)

    u = _fade(xf)
    v = _fade(yf)

    def grad(cx, cy, dx, dy):
        h = perm[(perm[cx % table_size] + cy) % table_size]
        return gx[h] * dx + gy[h] * dy

    n00 = grad(xi,     yi,     xf,     yf)
    n10 = grad(xi + 1, yi,     xf - 1, yf)
    n01 = grad(xi,     yi + 1, xf,     yf - 1)
    n11 = grad(xi + 1, yi + 1, xf - 1, yf - 1)

    return _lerp(_lerp(n00, n10, u), _lerp(n01, n11, u), v)


def fbm_heightmap(w: int, h: int, seed: int, octaves: int = 6) -> np.ndarray:
    """Generate FBM terrain heights in [0, 1] for a w×h grid."""
    px = np.tile(np.arange(w, dtype=np.float32), h).reshape(h, w)
    py = np.repeat(np.arange(h, dtype=np.float32), w).reshape(h, w)

    # Normalise to [0, 4] range for interesting variation
    sx = px / w * 4.0
    sy = py / h * 4.0

    result = np.zeros((h, w), dtype=np.float64)
    amplitude = 1.0
    frequency = 1.0
    total_amplitude = 0.0

    for i in range(octaves):
        noise = _gradient_noise_2d(sx * frequency, sy * frequency, seed=seed + i * 31337)
        result += noise * amplitude
        total_amplitude += amplitude
        amplitude *= 0.5
        frequency *= 2.0

    # Remap [-total_amplitude, +total_amplitude] → [0, 1]
    result = (result / total_amplitude + 1.0) * 0.5
    return np.clip(result, 0.0, 1.0).astype(np.float32)


# ---------------------------------------------------------------------------
# Ray generation
# ---------------------------------------------------------------------------

def _make_camera_basis(eye, target, world_up=(0.0, 0.0, -1.0)):
    """Return (right, up, forward) unit vectors.

    world_up defaults to (0,0,-1) instead of (0,1,0) to avoid the degenerate
    case when forward is (0,-1,0) — cross((0,-1,0),(0,1,0)) == zero vector.
    """
    eye = np.array(eye, dtype=np.float64)
    target = np.array(target, dtype=np.float64)
    up_hint = np.array(world_up, dtype=np.float64)

    forward = target - eye
    forward /= np.linalg.norm(forward)

    right = np.cross(forward, up_hint)
    norm_r = np.linalg.norm(right)
    if norm_r < 1e-9:
        # Fallback: try another axis
        up_hint = np.array([0.0, 0.0, 1.0])
        right = np.cross(forward, up_hint)
        norm_r = np.linalg.norm(right)
    right /= norm_r

    up = np.cross(right, forward)
    up /= np.linalg.norm(up)

    return right, up, forward


def generate_rays(tile_w: int, tile_h: int, eye, target, fov_deg: float = 60.0):
    """Return (origins, dirs) each shaped (tile_h, tile_w, 3) as float32."""
    right, up, forward = _make_camera_basis(eye, target)

    aspect = tile_w / tile_h
    half_h = np.tan(np.radians(fov_deg / 2.0))
    half_w = half_h * aspect

    # NDC coordinates [-1, 1]
    u_vals = np.linspace(-1.0, 1.0, tile_w, dtype=np.float64)
    v_vals = np.linspace(1.0, -1.0, tile_h, dtype=np.float64)   # top-to-bottom

    ug, vg = np.meshgrid(u_vals, v_vals)  # (tile_h, tile_w)

    # Direction vectors (un-normalised)
    dirs = (
        forward[np.newaxis, np.newaxis, :]
        + (ug[..., np.newaxis] * half_w) * right[np.newaxis, np.newaxis, :]
        + (vg[..., np.newaxis] * half_h) * up[np.newaxis, np.newaxis, :]
    )
    norms = np.linalg.norm(dirs, axis=-1, keepdims=True)
    dirs /= norms

    origins = np.broadcast_to(
        np.array(eye, dtype=np.float64)[np.newaxis, np.newaxis, :],
        (tile_h, tile_w, 3),
    ).copy()

    return origins.astype(np.float32), dirs.astype(np.float32)


# ---------------------------------------------------------------------------
# Write / read .bench
# ---------------------------------------------------------------------------

def write_bench(path: Path, origins: np.ndarray, dirs: np.ndarray,
                heights: np.ndarray, svdag_cells: np.ndarray) -> None:
    """Write a tile to *path* in .bench v1 format."""
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("wb") as f:
        # Header
        header = struct.pack(HEADER_FMT,
                             MAGIC, VERSION,
                             TILE_W, TILE_H,
                             1,   # ray_format
                             1,   # height_format
                             1,   # svdag_format
                             b"\x00" * 16)
        f.write(header)

        # Ray block — row-major, f32
        rays = np.concatenate([origins, dirs], axis=-1)  # (H, W, 6)
        f.write(rays.astype("<f4").tobytes())

        # Heightmap block
        f.write(heights.astype("<f4").tobytes())

        # SVDAG stub block — already packed as uint8 bytes
        f.write(svdag_cells.tobytes())

    actual = path.stat().st_size
    assert actual == EXPECTED_TOTAL, (
        f"Written file is {actual} bytes; expected {EXPECTED_TOTAL}. "
        "Check struct packing."
    )


def read_bench(path: Path):
    """Return (header_dict, origins, dirs, heights, svdag_cells)."""
    data = path.read_bytes()
    if len(data) != EXPECTED_TOTAL:
        raise ValueError(
            f"{path}: expected {EXPECTED_TOTAL} bytes, got {len(data)}"
        )

    # Header
    raw = struct.unpack_from(HEADER_FMT, data, 0)
    magic, version, tile_w, tile_h, ray_fmt, hgt_fmt, svdag_fmt, _ = raw
    hdr = {
        "magic": magic,
        "version": version,
        "tile_w": tile_w,
        "tile_h": tile_h,
        "ray_format": ray_fmt,
        "height_format": hgt_fmt,
        "svdag_format": svdag_fmt,
    }

    # Ray block
    off = HEADER_SIZE
    n = tile_w * tile_h
    rays_raw = np.frombuffer(data, dtype="<f4", count=n * 6, offset=off).reshape(n, 6)
    origins = rays_raw[:, :3].reshape(tile_h, tile_w, 3)
    dirs = rays_raw[:, 3:].reshape(tile_h, tile_w, 3)

    # Heightmap block
    off += n * RAY_SIZE
    heights = np.frombuffer(data, dtype="<f4", count=n, offset=off).reshape(tile_h, tile_w)

    # SVDAG stub block
    off += n * HEIGHT_SIZE
    # 8 × f32 per cell; field 2 (depth) and field 4 (child_mask) are u32 by spec
    # but stored in the same 32-byte slot.  We read all as bytes then view.
    svdag_bytes = data[off: off + n * SVDAG_SIZE]
    svdag_cells = np.frombuffer(svdag_bytes, dtype=np.uint8).reshape(n, SVDAG_SIZE)

    return hdr, origins, dirs, heights, svdag_cells


def decode_svdag_fields(svdag_cells: np.ndarray):
    """Return structured view of SVDAG fields.

    Returns a dict of (N,) arrays for:
      node_min_sdf, ray_dist_to_near, depth, node_extent,
      child_mask, parent_sdf, reserved1, reserved2
    """
    n = svdag_cells.shape[0]
    flat = svdag_cells.reshape(n, SVDAG_SIZE)

    fields = {}
    float_view = flat.view("<f4").reshape(n, 8)
    uint_view = flat.view("<u4").reshape(n, 8)

    fields["node_min_sdf"]      = float_view[:, 0]
    fields["ray_dist_to_near"]  = float_view[:, 1]
    fields["depth"]             = uint_view[:, 2]
    fields["node_extent"]       = float_view[:, 3]
    fields["child_mask"]        = uint_view[:, 4]
    fields["parent_sdf"]        = float_view[:, 5]
    fields["reserved1"]         = float_view[:, 6]
    fields["reserved2"]         = float_view[:, 7]
    return fields


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_generate_synthetic(args):
    output = Path(args.output)
    seed = args.seed

    print(f"Generating synthetic tile (seed={seed}) → {output}")

    # Heights via FBM
    heights = fbm_heightmap(TILE_W, TILE_H, seed=seed)

    # Rays: camera at (512, 200, 512) looking toward (512, 0, 512), FOV 60°
    eye = (512.0, 200.0, 512.0)
    target = (512.0, 0.0, 512.0)
    origins, dirs = generate_rays(TILE_W, TILE_H, eye, target, fov_deg=60.0)

    # SVDAG stubs
    # Layout: node_min_sdf(f32), ray_dist_to_near(f32), depth(u32),
    #          node_extent(f32), child_mask(u32), parent_sdf(f32),
    #          reserved1(f32), reserved2(f32)
    n = N_RAYS
    rng = np.random.default_rng(seed + 1)

    h_flat = heights.reshape(n).astype(np.float32)

    node_min_sdf     = (h_flat * 400.0).astype(np.float32)      # TEngine scale

    # ray_dist_to_near: plausible camera-to-surface distance in [50, 250]
    ray_dist_to_near = (200.0 - h_flat * 150.0 + rng.uniform(-20, 20, n)).astype(np.float32)
    ray_dist_to_near = np.clip(ray_dist_to_near, 10.0, 500.0)

    # depth 0-7 based on grid position (a checkerboard-ish split)
    grid_x = (np.arange(n) % TILE_W).astype(np.float32)
    grid_y = (np.arange(n) // TILE_W).astype(np.float32)
    depth_f = (grid_x / TILE_W * 4 + grid_y / TILE_H * 4).astype(np.float32)
    depth = np.clip(depth_f, 0, 7).astype(np.uint32)

    node_extent = (400.0 / (2.0 ** depth.astype(np.float32))).astype(np.float32)

    child_mask  = rng.integers(0, 256, n, dtype=np.uint32)
    parent_sdf  = (node_min_sdf * 1.5).astype(np.float32)
    reserved1   = np.zeros(n, dtype=np.float32)
    reserved2   = np.zeros(n, dtype=np.float32)

    # Pack into uint8 blob so u32 fields stay as u32
    svdag_cells = np.zeros((n, SVDAG_SIZE), dtype=np.uint8)
    float_view  = svdag_cells.view("<f4").reshape(n, 8)
    uint_view   = svdag_cells.view("<u4").reshape(n, 8)

    float_view[:, 0] = node_min_sdf
    float_view[:, 1] = ray_dist_to_near
    uint_view[:,  2] = depth
    float_view[:, 3] = node_extent
    uint_view[:,  4] = child_mask
    float_view[:, 5] = parent_sdf
    float_view[:, 6] = reserved1
    float_view[:, 7] = reserved2

    write_bench(output, origins, dirs, heights, svdag_cells)

    size = output.stat().st_size
    print(f"Written: {size:,} bytes ({size} == {EXPECTED_TOTAL}: {size == EXPECTED_TOTAL})")
    print(f"Heights range: [{heights.min():.3f}, {heights.max():.3f}]")
    print(f"node_min_sdf range: [{node_min_sdf.min():.1f}, {node_min_sdf.max():.1f}]")
    print(f"ray_dist_to_near range: [{ray_dist_to_near.min():.1f}, {ray_dist_to_near.max():.1f}]")
    print(f"depth range: [{depth.min()}, {depth.max()}]")


def cmd_info(args):
    path = Path(args.bench_file)
    hdr, origins, dirs, heights, svdag_cells = read_bench(path)

    fields = decode_svdag_fields(svdag_cells)

    print(f"File:         {path}")
    print(f"Size:         {path.stat().st_size:,} bytes")
    print(f"Magic:        {hdr['magic']}")
    print(f"Version:      {hdr['version']}")
    print(f"Tile:         {hdr['tile_w']}×{hdr['tile_h']}")
    print(f"ray_format:   {hdr['ray_format']}")
    print(f"height_fmt:   {hdr['height_format']}")
    print(f"svdag_fmt:    {hdr['svdag_format']}")
    print()
    print(f"Heights:      min={heights.min():.4f}  max={heights.max():.4f}  mean={heights.mean():.4f}")
    print()
    print("Sample rays (row 0, cols 0–3):")
    for col in range(4):
        o = origins[0, col]
        d = dirs[0, col]
        print(f"  [{col}] origin=({o[0]:.2f}, {o[1]:.2f}, {o[2]:.2f})  "
              f"dir=({d[0]:.4f}, {d[1]:.4f}, {d[2]:.4f})")
    print()
    print("Sample SVDAG cells (first 4):")
    for i in range(4):
        print(f"  [{i}] node_min_sdf={fields['node_min_sdf'][i]:.2f}  "
              f"ray_dist={fields['ray_dist_to_near'][i]:.2f}  "
              f"depth={fields['depth'][i]}  "
              f"extent={fields['node_extent'][i]:.2f}  "
              f"child_mask={fields['child_mask'][i]:#04x}")


def cmd_replay(args):
    path = Path(args.bench_file)
    iters = args.iters
    func_name = args.function

    hdr, origins, dirs, heights, svdag_cells = read_bench(path)
    fields = decode_svdag_fields(svdag_cells)

    n = N_RAYS
    node_min_sdf    = fields["node_min_sdf"]
    ray_dist        = fields["ray_dist_to_near"]
    depth           = fields["depth"]
    node_extent     = fields["node_extent"]

    # Select predicate
    if func_name == "naive":
        def should_skip(i):
            return False  # never skip — baseline

    elif func_name == "aggressive":
        def should_skip(i):
            # Skip if the minimum SDF value at the node exceeds the current
            # ray distance — the ray has already passed the nearest surface.
            return bool(node_min_sdf[i] > ray_dist[i])
    else:
        print(f"Unknown function '{func_name}'. Choose: naive | aggressive",
              file=sys.stderr)
        sys.exit(1)

    print(f"Replay: {path.name}  function={func_name}  iters={iters}")
    print(f"Tile: {hdr['tile_w']}×{hdr['tile_h']} = {n} rays")
    print()

    skipped_total = 0
    processed_total = 0

    t0 = time.perf_counter()
    for _ in range(iters):
        skip_count = 0
        for i in range(n):
            if should_skip(i):
                skip_count += 1
        skipped_total += skip_count
        processed_total += n
    t1 = time.perf_counter()

    wall_s = t1 - t0
    skip_rate = skipped_total / processed_total
    ns_per_ray = (wall_s / processed_total) * 1e9

    print(f"Rays processed:    {processed_total:,}")
    print(f"Rays skipped:      {skipped_total:,} ({skip_rate * 100:.1f}%)")
    print(f"Wall-clock time:   {wall_s * 1000:.2f} ms")
    print(f"Throughput:        {ns_per_ray:.1f} ns/ray")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="tengine_bench_capture",
        description=(
            "Tile benchmark capture and replay for FunSearch ESS predicates. "
            "Manages 64×64 tile .bench files: generate, inspect, or replay "
            "should_skip() candidates against captured ray/SVDAG tile data. "
            "Use 'generate-synthetic' to create a tile without a live TEngine session."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # generate-synthetic
    p_gen = sub.add_parser(
        "generate-synthetic",
        help="Generate a synthetic tile .bench file with FBM terrain and realistic ray data.",
    )
    _default_output = str(
        Path(__file__).resolve().parent.parent / "benchmarks" / "tiles" / "sample.bench"
    )
    p_gen.add_argument(
        "--output", default=_default_output,
        help=f"Output path for the tile .bench file (default: {_default_output})",
    )
    p_gen.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for reproducible tile generation (default: 42)",
    )
    p_gen.set_defaults(func=cmd_generate_synthetic)

    # info
    p_info = sub.add_parser(
        "info",
        help="Print tile header info and sample ray/SVDAG values from a .bench file.",
    )
    p_info.add_argument("bench_file", help="Path to a .bench tile file")
    p_info.set_defaults(func=cmd_info)

    # replay
    p_rep = sub.add_parser(
        "replay",
        help=(
            "Replay a tile .bench file with an ESS predicate and measure timing. "
            "Reports skip rate and ns/ray for FunSearch candidate scoring."
        ),
    )
    p_rep.add_argument("bench_file", help="Path to a .bench tile file")
    p_rep.add_argument(
        "--iters", type=int, default=100,
        help="Number of replay iterations for stable timing (default: 100)",
    )
    p_rep.add_argument(
        "--function", choices=["naive", "aggressive"], default="naive",
        help=(
            "ESS predicate to replay: "
            "'naive' = never skip (baseline), "
            "'aggressive' = skip if node_min_sdf > ray_dist (default: naive)"
        ),
    )
    p_rep.set_defaults(func=cmd_replay)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
