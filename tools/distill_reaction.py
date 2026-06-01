#!/usr/bin/env python3
"""Distill evolved FunSearch reaction kernels to tiny MLPs for StructuredBuffer deployment.

Trains a [N → 32 → 32 → 1] MLP on samples from the top-K evolved programs.
Exports float16 weights as .npy + metadata JSON for uploading to a StructuredBuffer
in a Slang/HLSL/WGPU compute shader.

Usage:
    # Phase kernel (2-input: phi, temp)
    python tools/distill_reaction.py phase \\
        benchmarks/curriculum/2026-05-31/phase_results_gen36.json

    # Latent kernel (3-input: phi, temp, lap_T)
    python tools/distill_reaction.py latent \\
        benchmarks/curriculum/2026-05-31/latent_results_gen15.json

Output: benchmarks/luts/<name>_mlp_<timestamp>.{npy,json}
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LUT_DIR   = REPO_ROOT / "benchmarks" / "luts"
sys.path.insert(0, str(REPO_ROOT))

try:
    import numpy as np
    import torch
    import torch.nn as nn
    TORCH_OK = True
except ImportError:
    TORCH_OK = False


# ---------------------------------------------------------------------------
# Sampling from C++ evaluator
# ---------------------------------------------------------------------------

def sample_kernel(kernel_type: str, code: str, n_samples: int = 50000,
                  seed: int = 12345) -> tuple:
    """Sample (inputs, outputs) from an evolved C++ kernel.

    Returns: (X: np.ndarray [n, d_in], y: np.ndarray [n])

    Pass different seeds when pooling multiple programs so top-K pooling
    produces genuinely diverse training points rather than duplicate grids.
    """
    import subprocess, tempfile

    if kernel_type == "phase":
        wrapper = """
#include <cmath>
#include <cstdio>
using namespace std;
{code}
int main() {{
    int N = {N};
    unsigned int seed = {SEED}u;
    for (int i = 0; i < N; i++) {{
        seed = seed * 1664525u + 1013904223u;
        float phi  = (float)(seed & 0xffff) / 65535.f;
        seed = seed * 1664525u + 1013904223u;
        float temp = (float)(seed & 0xffff) / 65535.f;
        float r = reaction(phi, temp);
        if (!isfinite(r)) r = 0.f;
        printf("%f %f %f\\n", phi, temp, r);
    }}
    return 0;
}}
""".format(code=code, N=n_samples, SEED=seed)
        n_in = 2

    elif kernel_type == "latent":
        # Feature stored as NORMALIZED lap_T ∈ [0,1] to match NeuralReaction.slang
        # which feeds lap_T_norm = clamp((lap_T + 8) / 16, 0, 1) to the network.
        wrapper = """
#include <cmath>
#include <cstdio>
using namespace std;
{code}
int main() {{
    int N = {N};
    unsigned int seed = {SEED}u;
    for (int i = 0; i < N; i++) {{
        seed = seed * 1664525u + 1013904223u;
        float phi   = (float)(seed & 0xffff) / 65535.f;
        seed = seed * 1664525u + 1013904223u;
        float temp  = (float)(seed & 0xffff) / 65535.f;
        seed = seed * 1664525u + 1013904223u;
        float lap_T_norm = (float)(seed & 0xffff) / 65535.f;  // [0, 1]
        float lap_T = lap_T_norm * 16.f - 8.f;                // [-8, 8] for reaction()
        float r = reaction(phi, temp, lap_T);
        if (!isfinite(r)) r = 0.f;
        // Store NORMALIZED lap_T as feature (matches Slang inference)
        printf("%f %f %f %f\\n", phi, temp, lap_T_norm, r);
    }}
    return 0;
}}
""".format(code=code, N=n_samples, SEED=seed)
        n_in = 3

    else:
        raise ValueError(f"Unknown kernel type: {kernel_type}")

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "sample.cpp"
        exe = Path(td) / "sample"
        src.write_text(wrapper)
        result = subprocess.run(
            ["g++", "-O2", "-o", str(exe), str(src), "-lm"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Compile failed:\n{result.stderr[:500]}")
        out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            raise RuntimeError(f"Run failed:\n{out.stderr[:200]}")

    rows = [list(map(float, line.split())) for line in out.stdout.strip().split("\n") if line.strip()]
    data = np.array(rows, dtype=np.float32)
    X = data[:, :n_in]
    y = data[:, n_in]
    return X, y


# ---------------------------------------------------------------------------
# Tiny MLP
# ---------------------------------------------------------------------------

class ReactionMLP(nn.Module):
    def __init__(self, n_in: int, hidden: int = 32):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_in, hidden), nn.ReLU(),
            nn.Linear(hidden, hidden), nn.ReLU(),
            nn.Linear(hidden, 1),
        )

    def forward(self, x):
        return self.net(x).squeeze(-1)


def train_mlp(X: "np.ndarray", y: "np.ndarray",
              n_in: int, hidden: int = 32,
              epochs: int = 3000, lr: float = 1e-3,
              test_frac: float = 0.1) -> tuple:
    """Train MLP; return (model, max_abs_error_on_test, train_loss)."""
    split = int(len(X) * (1 - test_frac))
    X_tr, y_tr = X[:split], y[:split]
    X_te, y_te = X[split:], y[split:]

    model = ReactionMLP(n_in, hidden)
    opt   = torch.optim.Adam(model.parameters(), lr=lr)

    X_tr_t = torch.from_numpy(X_tr)
    y_tr_t = torch.from_numpy(y_tr)

    for ep in range(epochs):
        opt.zero_grad()
        pred = model(X_tr_t)
        loss = nn.functional.mse_loss(pred, y_tr_t)
        loss.backward()
        opt.step()
        if ep % 500 == 499:
            print(f"  epoch {ep+1}/{epochs}  train_mse={loss.item():.6f}")

    with torch.no_grad():
        pred_te = model(torch.from_numpy(X_te)).numpy()
    max_err = float(np.abs(pred_te - y_te).max())
    return model, max_err, float(loss.item())


def export_weights(model: "ReactionMLP") -> "np.ndarray":
    """Flatten all model weights to a float16 1D array.

    Layout (row-major, matches NeuralReaction.slang):
      W1[n_in  × hidden] + b1[hidden]
      W2[hidden × hidden] + b2[hidden]
      W3[1     × hidden] + b3[1]
    """
    parts = []
    for name, param in model.named_parameters():
        parts.append(param.detach().cpu().float().numpy().flatten())
    return np.concatenate(parts).astype(np.float16)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Distill FunSearch kernels to tiny MLP")
    parser.add_argument("kernel_type", choices=["phase", "latent"])
    parser.add_argument("results_json")
    parser.add_argument("--top-k",      type=int,   default=5, dest="top_k",
                        help="Pool training data from top-K programs (default 5)")
    parser.add_argument("--hidden",     type=int,   default=32)
    parser.add_argument("--n-samples",  type=int,   default=8000,
                        help="Samples per program (default 8000; total = top_k × n_samples)")
    parser.add_argument("--epochs",     type=int,   default=1000)
    args = parser.parse_args()

    if not TORCH_OK:
        print("PyTorch not available. Install: pip install torch numpy")
        return 1

    with open(args.results_json) as f:
        data = json.load(f)

    programs = data.get("top_programs", [])
    if not programs:
        print("No top_programs in results JSON")
        return 1

    n_in = 2 if args.kernel_type == "phase" else 3
    k = min(args.top_k, len(programs))
    print(f"[distill] kernel={args.kernel_type}  top-K={k}  architecture: [{n_in} → {args.hidden} → {args.hidden} → 1]")

    # --- Pool training data from top-K programs ---
    print(f"\n[distill] sampling {args.n_samples} points from each of {k} program(s)...")
    t0 = time.time()
    all_X = []
    all_y = []
    best_fitness = 0.0

    for i, prog in enumerate(programs[:k]):
        code = prog.get("reaction_code", "")
        fitness = float(prog.get("fitness", 0))
        if not code:
            print(f"  skip program {i}: missing reaction_code")
            continue
        if i == 0:
            best_fitness = fitness
        print(f"  program {i}: fitness={fitness:.4f}  ({len(code)} chars)")
        try:
            # Use a unique seed per program so pooled training data is diverse
            prog_seed = 12345 + i * 7919
            X_i, y_i = sample_kernel(args.kernel_type, code, n_samples=args.n_samples,
                                     seed=prog_seed)
            all_X.append(X_i)
            all_y.append(y_i)
        except Exception as e:
            print(f"  WARN: sampling failed for program {i}: {e}")

    if not all_X:
        print("Error: no training data collected")
        return 1

    X = np.concatenate(all_X)
    y = np.concatenate(all_y)
    print(f"[distill] {len(X)} total training points in {time.time()-t0:.1f}s")
    print(f"  y range=[{y.min():.3f}, {y.max():.3f}]  mean={y.mean():.3f}")

    print(f"\n[distill] training {args.epochs} epochs...")
    t0 = time.time()
    model, max_err, train_loss = train_mlp(X, y, n_in=n_in, hidden=args.hidden, epochs=args.epochs)
    print(f"[distill] done in {time.time()-t0:.1f}s  train_mse={train_loss:.6f}  max_test_err={max_err:.6f}")

    weights = export_weights(model)
    n_params = len(weights)
    kb = n_params * 2 / 1024  # float16 = 2 bytes
    print(f"[distill] {n_params} params  {kb:.1f} KB float16")

    LUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    stem = f"{args.kernel_type}_mlp_{ts}"
    npy_path  = LUT_DIR / f"{stem}.npy"
    meta_path = LUT_DIR / f"{stem}.json"

    np.save(str(npy_path), weights)
    meta = {
        "kernel_type": args.kernel_type,
        "n_in": n_in,
        "hidden": args.hidden,
        "architecture": [n_in, args.hidden, args.hidden, 1],
        "n_params": n_params,
        "file_bytes": n_params * 2,
        "max_test_error": float(max_err),
        "train_mse": float(train_loss),
        "source_fitness": best_fitness,
        "source_file": str(args.results_json),
        "top_k_used": k,
        "weight_layout": "W1[hidden×n_in] b1[hidden] W2[hidden×hidden] b2[hidden] W3[1×hidden] b3[1] — float16 row-major",
        "inference": "See tools/NeuralReaction.slang",
        "timestamp": ts,
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    print(f"\n[distill] Output:")
    print(f"  {npy_path}  ({kb:.1f} KB float16)")
    print(f"  {meta_path}")
    print(f"\n[distill] Upload to GPU (Python):")
    print(f"  weights = np.load('{npy_path}').astype(np.float32)")
    print(f"  # Upload to StructuredBuffer<float16_t> in Slang shader")
    print(f"  # See tools/NeuralReaction.slang for inference code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
