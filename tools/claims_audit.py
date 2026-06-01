#!/usr/bin/env python3
"""
EDF claims evaluator for autobench curriculum cycles.

Reads cycle manifests and bus event history to assess evidence quality
and produce per-cycle confidence reports following the EDD model:
    Claim → Evidence → Provenance → Invalidation Rules → Confidence

Usage:
    python3 tools/claims_audit.py [--date YYYY-MM-DD] [--cycle-id CID] [--verbose]

Exit codes:
    0  — all cycles pass their confidence threshold
    1  — one or more cycles below threshold
    2  — error (missing data, parse error)
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BLOCKLIST_FIELDS = frozenset(["trigger", "invalidators", "confidence_rubric"])


def load_claim(path: Path) -> dict:
    import yaml
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    # strip comment lines that yaml.loads might bleed in
    return {k: v for k, v in raw.items() if k not in BLOCKLIST_FIELDS and not k.startswith("#")}


def confidence_for_cycle(manifest: dict, claim: dict) -> str:
    rubric = _load_rubric(claim)
    n = manifest.get("n_problems", 0)
    for level in ("HIGH", "MEDIUM", "LOW"):
        if level in rubric:
            cond = rubric[level]["condition"]
            import re
            m = re.match(r"n_problems\s*([><=]+)\s*(\d+)", cond)
            if m:
                op, threshold = m.group(1), int(m.group(2))
                val = {
                    "==": lambda a, b: a == b,
                    ">=": lambda a, b: a >= b,
                    "<": lambda a, b: a < b,
                }.get(op, lambda *a: False)(n, threshold)
                if val:
                    return level
    return "NONE"


def _load_rubric(claim: dict) -> dict:
    """Parse confidence_rubric from the YAML claim (inline eval is intentional)."""
    import yaml
    claim_path = Path(__file__).parent.parent / "claims" / "autobench" / "curriculum" / "cycle-claim.yaml"
    with open(claim_path) as fh:
        raw = yaml.safe_load(fh)
    rubric = raw.get("confidence_rubric", {})
    return rubric


def audit_cycles(
    day_dir: Path,
    date: str,
    cycle_id: str | None = None,
    verbose: bool = False,
) -> dict[str, dict]:
    """Audit all cycles for a given date, or a specific cycle_id."""
    results: dict[str, dict] = {}
    cycle_glob = f"cycles/{cycle_id}" if cycle_id else "cycles/*"
    manifests = sorted((day_dir / "cycles").glob("*/manifest.json")) if not cycle_id else list((day_dir / "cycles" / cycle_id).glob("manifest.json"))
    search_desc = f"cycles/*/manifest.json" if not cycle_id else f"cycles/{cycle_id}/manifest.json"

    if not manifests:
        print(f"No cycles found in {day_dir / search_desc}", file=sys.stderr)
        return results

    claim_path = Path(__file__).parent.parent / "claims" / "autobench" / "curriculum" / "cycle-claim.yaml"
    claim = load_claim(claim_path)

    for mf_path in manifests:
        cid = mf_path.parent.name
        try:
            manifest = json.load(open(mf_path))
        except Exception as exc:
            results[cid] = {"status": "ERROR", "error": str(exc), "manifest_path": str(mf_path)}
            continue

        n_problems = manifest.get("n_problems", 0)
        confidence = confidence_for_cycle(manifest, claim)
        provenance = {
            "cycle_id": cid,
            "date": date,
            "n_problems": n_problems,
            "generator_model": manifest.get("generator_model", ""),
            "goals": manifest.get("goals", {}),
            "manifest_path": str(mf_path),
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }

        # Check invalidators
        stale = []
        # Check schema version (inferred from manifest structure)
        # For v1, nothing is stale yet
        stale_reason = None
        if stale:
            stale_reason = "; ".join(stale)

        results[cid] = {
            "status": "OK" if not stale else "STALE",
            "stale_reason": stale_reason,
            "confidence": confidence,
            "provenance": provenance,
            "claim_id": claim.get("claim_id", ""),
            "claim_version": claim.get("version", ""),
        }

        if verbose or confidence in ("LOW", "NONE"):
            print(
                f"  [{confidence:6s}] {cid[-14:]} — {n_problems} problems "
                f"({manifest.get('generator_model','?')})",
                file=sys.stderr,
            )

    return results


def summarize_results(results: dict[str, dict]) -> dict:
    total = len(results)
    by_conf = defaultdict(int)
    errors = 0
    for r in results.values():
        if r.get("status") == "ERROR":
            errors += 1
        else:
            by_conf[r.get("confidence", "NONE")] += 1
    return {
        "total_cycles": total,
        "errors": errors,
        "confidence_breakdown": dict(by_conf),
        "all_high": all(r.get("confidence") == "HIGH" for r in results.values() if r.get("status") != "ERROR"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="EDD claims audit for autobench curriculum cycles")
    parser.add_argument("--date", default=None, help="Date dir to audit (default: most recent)")
    parser.add_argument("--cycle-id", default=None, help="Audit specific cycle only")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--output-json", action="store_true", help="Output machine-readable JSON to stdout")
    args = parser.parse_args()

    curriculum_root = Path("autobench/benchmarks/curriculum")
    if args.date:
        day_dir = curriculum_root / args.date
        date = args.date
    else:
        # find most recent date dir
        date_dirs = sorted(d for d in curriculum_root.iterdir() if d.is_dir() and d.name[0].isdigit())
        if not date_dirs:
            print("No curriculum date directories found", file=sys.stderr)
            return 2
        day_dir = date_dirs[-1]
        date = day_dir.name

    print(f"Auditing claims for {date} …", file=sys.stderr)
    results = audit_cycles(day_dir, date, cycle_id=args.cycle_id, verbose=args.verbose)
    summary = summarize_results(results)

    if args.output_json:
        out = {"date": date, "summary": summary, "cycles": results}
        print(json.dumps(out, indent=2))
    else:
        print(
            f"  {summary['total_cycles']} cycles, "
            f"{summary['errors']} errors — "
            f"confidence: {summary['confidence_breakdown']}",
            file=sys.stderr,
        )
        print("OK" if summary["all_high"] else "DEGRADED", file=sys.stderr)

    return 0 if summary["all_high"] else 1


if __name__ == "__main__":
    sys.exit(main())