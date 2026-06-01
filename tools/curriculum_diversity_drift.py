#!/usr/bin/env python3
"""
Curriculum diversity-drift analyzer.

Reads every day under autobench/benchmarks/curriculum/<YYYY-MM-DD>/cases.jsonl,
computes multi-modal similarity (TF-IDF cosine + char-5gram Jaccard + skill-set
Jaccard) across all problem pairs, and reports:

  - per-day intra-similarity stats (alarm on template collapse)
  - cross-day drift (mean similarity between day pairs)
  - skill-label distribution + Jensen-Shannon divergence between days
  - difficulty distribution + Kolmogorov-Smirnov-style drift
  - top-K nearest pairs (within & across days) — surfaces actual duplicates
  - per-problem novelty score (mean distance from everything else)
  - the least-novel ("template-y") and most-novel ("outlier") problems

Why both TF-IDF and 5-gram Jaccard: TF-IDF normalizes away stop-words and
catches semantic recurrence (same vocabulary), while char-5gram Jaccard catches
phrase-level templates that TF-IDF tokenization eats. If they agree, you have
a real duplicate. If only 5-gram is high, you have a "boilerplate phrasing"
template (e.g. "Given an array of n integers..."). If only TF-IDF is high,
you have semantic recurrence in different wording — still bad, just subtler.

Usage:
    python3 tools/curriculum_diversity_drift.py
    python3 tools/curriculum_diversity_drift.py --root autobench/benchmarks/curriculum --json out.json
    python3 tools/curriculum_diversity_drift.py --top-k 10 --alarm-threshold 0.5
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


# ────────────────────────────────────────────────────────────────────────────
# data
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class Problem:
    day: str            # "2026-05-17"
    pid: str            # "prob-003"
    prompt: str         # full statement
    skills: list[str]   # ["arrays", "two-pointers"]
    difficulty: int     # 800
    rationale: str
    novelty: float = 0.0   # filled in later

    @property
    def key(self) -> str:
        return f"{self.day}/{self.pid}"


def load_all(root: Path) -> list[Problem]:
    problems: list[Problem] = []
    day_dirs = sorted(
        d for d in root.iterdir()
        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)
    )
    for day_dir in day_dirs:
        cases = day_dir / "cases.jsonl"
        if not cases.exists():
            continue
        for line in cases.read_text().splitlines():
            if not line.strip():
                continue
            j = json.loads(line)
            meta = j.get("metadata", {})
            problems.append(Problem(
                day=day_dir.name,
                pid=j.get("id", "?"),
                prompt=j.get("prompt", ""),
                skills=list(meta.get("target_skills", [])),
                difficulty=int(meta.get("difficulty_rating", 0)),
                rationale=str(meta.get("rationale", "")),
            ))
    return problems


# ────────────────────────────────────────────────────────────────────────────
# similarity kernels
# ────────────────────────────────────────────────────────────────────────────

def tfidf_matrix(texts: list[str]) -> np.ndarray:
    """TF-IDF cosine similarity matrix (NxN)."""
    vec = TfidfVectorizer(
        ngram_range=(1, 2),
        min_df=1,
        max_df=0.95,
        sublinear_tf=True,
        stop_words="english",
    )
    x = vec.fit_transform(texts)
    return cosine_similarity(x)


def char_ngrams(s: str, n: int = 5) -> set[str]:
    s = re.sub(r"\s+", " ", s.lower())
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else set()


def jaccard_matrix(texts: list[str], n: int = 5) -> np.ndarray:
    grams = [char_ngrams(t, n) for t in texts]
    N = len(texts)
    m = np.zeros((N, N))
    for i in range(N):
        for j in range(i, N):
            a, b = grams[i], grams[j]
            if not a and not b:
                v = 0.0
            else:
                inter = len(a & b)
                union = len(a | b)
                v = inter / union if union else 0.0
            m[i, j] = m[j, i] = v
    return m


def skillset_jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ────────────────────────────────────────────────────────────────────────────
# distribution drift (numpy-only — no scipy)
# ────────────────────────────────────────────────────────────────────────────

def js_divergence(p: dict[str, float], q: dict[str, float], base: float = 2.0) -> float:
    """Jensen-Shannon divergence between two label distributions. 0 = identical, 1 = disjoint."""
    keys = sorted(set(p) | set(q))
    if not keys:
        return 0.0
    pv = np.array([p.get(k, 0.0) for k in keys], dtype=float)
    qv = np.array([q.get(k, 0.0) for k in keys], dtype=float)
    if pv.sum() == 0 or qv.sum() == 0:
        return 1.0
    pv /= pv.sum()
    qv /= qv.sum()
    m = (pv + qv) / 2
    def kl(a, b):
        mask = a > 0
        return float(np.sum(a[mask] * np.log(a[mask] / np.where(b[mask] > 0, b[mask], 1e-12)) / np.log(base)))
    return 0.5 * kl(pv, m) + 0.5 * kl(qv, m)


def ks_distance(xs: list[float], ys: list[float]) -> float:
    """Two-sample Kolmogorov-Smirnov distance: max |CDF_x(t) - CDF_y(t)|. 0 = identical, 1 = disjoint."""
    if not xs or not ys:
        return 0.0
    pts = sorted(set(xs) | set(ys))
    xs_sorted = sorted(xs)
    ys_sorted = sorted(ys)
    def cdf(arr, t):
        # fraction of arr <= t
        lo, hi = 0, len(arr)
        while lo < hi:
            mid = (lo + hi) // 2
            if arr[mid] <= t:
                lo = mid + 1
            else:
                hi = mid
        return lo / len(arr)
    return max(abs(cdf(xs_sorted, t) - cdf(ys_sorted, t)) for t in pts)


# ────────────────────────────────────────────────────────────────────────────
# analysis
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class DayStats:
    day: str
    n: int
    mean_tfidf: float
    max_tfidf: float
    mean_5gram: float
    max_5gram: float
    n_unique_skills: int
    difficulty_min: int
    difficulty_max: int
    difficulty_mean: float
    skill_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class PairSim:
    a_key: str
    b_key: str
    tfidf: float
    fivegram: float
    skill_overlap: float
    same_day: bool

    @property
    def composite(self) -> float:
        # 0.55*TFIDF + 0.35*5gram + 0.10*skill_overlap
        return 0.55 * self.tfidf + 0.35 * self.fivegram + 0.10 * self.skill_overlap


def upper_offdiag(m: np.ndarray) -> np.ndarray:
    """Upper-triangle excluding diagonal — for non-redundant pairwise stats."""
    iu = np.triu_indices_from(m, k=1)
    return m[iu]


def analyze(problems: list[Problem]) -> tuple[list[DayStats], list[PairSim], dict]:
    if len(problems) < 2:
        return [], [], {"error": "need at least 2 problems"}

    texts = [p.prompt for p in problems]
    days = sorted({p.day for p in problems})

    tfidf = tfidf_matrix(texts)
    five = jaccard_matrix(texts, n=5)

    # per-day stats
    day_stats: list[DayStats] = []
    day_to_idx: dict[str, list[int]] = {d: [i for i, p in enumerate(problems) if p.day == d] for d in days}
    for d in days:
        idx = day_to_idx[d]
        if len(idx) < 2:
            sub_t = sub_f = np.array([0.0])
        else:
            sub_t = upper_offdiag(tfidf[np.ix_(idx, idx)])
            sub_f = upper_offdiag(five[np.ix_(idx, idx)])
        skills = [s for i in idx for s in problems[i].skills]
        difficulties = [problems[i].difficulty for i in idx]
        day_stats.append(DayStats(
            day=d,
            n=len(idx),
            mean_tfidf=float(np.mean(sub_t)),
            max_tfidf=float(np.max(sub_t)),
            mean_5gram=float(np.mean(sub_f)),
            max_5gram=float(np.max(sub_f)),
            n_unique_skills=len(set(skills)),
            difficulty_min=min(difficulties) if difficulties else 0,
            difficulty_max=max(difficulties) if difficulties else 0,
            difficulty_mean=float(np.mean(difficulties)) if difficulties else 0.0,
            skill_counts=dict(Counter(skills).most_common()),
        ))

    # all pairs (for top-K, novelty)
    pairs: list[PairSim] = []
    N = len(problems)
    for i in range(N):
        for j in range(i + 1, N):
            pairs.append(PairSim(
                a_key=problems[i].key,
                b_key=problems[j].key,
                tfidf=float(tfidf[i, j]),
                fivegram=float(five[i, j]),
                skill_overlap=skillset_jaccard(problems[i].skills, problems[j].skills),
                same_day=(problems[i].day == problems[j].day),
            ))

    # novelty score per problem = 1 - mean(composite sim to all others)
    composite = 0.55 * tfidf + 0.35 * five
    np.fill_diagonal(composite, 0.0)
    novelty = 1.0 - (composite.sum(axis=1) / (N - 1))
    for i, p in enumerate(problems):
        p.novelty = float(novelty[i])

    # cross-day drift
    cross_day: list[dict] = []
    for i, da in enumerate(days):
        for db in days[i + 1:]:
            ia, ib = day_to_idx[da], day_to_idx[db]
            block_t = tfidf[np.ix_(ia, ib)]
            block_f = five[np.ix_(ia, ib)]
            sk_a = Counter(s for i in ia for s in problems[i].skills)
            sk_b = Counter(s for i in ib for s in problems[i].skills)
            diff_a = [problems[i].difficulty for i in ia]
            diff_b = [problems[i].difficulty for i in ib]
            cross_day.append({
                "from": da,
                "to": db,
                "mean_tfidf": float(np.mean(block_t)) if block_t.size else 0.0,
                "max_tfidf": float(np.max(block_t)) if block_t.size else 0.0,
                "mean_5gram": float(np.mean(block_f)) if block_f.size else 0.0,
                "skills_js": js_divergence(sk_a, sk_b),
                "difficulty_ks": ks_distance(diff_a, diff_b),
            })

    extras = {
        "novelty_ranking": sorted(
            [{"key": p.key, "novelty": p.novelty, "prompt_head": p.prompt[:80]} for p in problems],
            key=lambda x: x["novelty"],
        ),
        "cross_day": cross_day,
    }
    return day_stats, pairs, extras


# ────────────────────────────────────────────────────────────────────────────
# rendering
# ────────────────────────────────────────────────────────────────────────────

def bar(v: float, width: int = 20, full: float = 1.0) -> str:
    n = int(round((v / full) * width)) if full > 0 else 0
    return "█" * n + "·" * (width - n)


def render(day_stats: list[DayStats], pairs: list[PairSim], extras: dict,
           alarm_threshold: float, top_k: int) -> str:
    lines: list[str] = []
    lines.append("─" * 78)
    lines.append("  Curriculum Diversity Drift Report")
    lines.append("─" * 78)
    lines.append(f"  Days: {len(day_stats)}   Total problems: {sum(d.n for d in day_stats)}")
    lines.append("")

    # per-day
    lines.append("  PER-DAY STATS")
    lines.append("  " + "─" * 76)
    lines.append(f"  {'date':<12} {'n':>3} {'mean(TF)':>9} {'max(TF)':>8} {'mean(5g)':>9} {'max(5g)':>8} {'#skills':>8} {'diff':>10}")
    for ds in day_stats:
        flag = ""
        if ds.max_tfidf >= alarm_threshold or ds.max_5gram >= alarm_threshold:
            flag = " ⚠"
        lines.append(
            f"  {ds.day:<12} {ds.n:>3} "
            f"{ds.mean_tfidf:>9.3f} {ds.max_tfidf:>8.3f} "
            f"{ds.mean_5gram:>9.3f} {ds.max_5gram:>8.3f} "
            f"{ds.n_unique_skills:>8} "
            f"{ds.difficulty_min}-{ds.difficulty_max:<5}{flag}"
        )
    lines.append("")

    # alarms
    alarms: list[str] = []
    for ds in day_stats:
        if ds.max_tfidf >= alarm_threshold:
            alarms.append(f"  ⚠ {ds.day}: max intra-day TFIDF={ds.max_tfidf:.3f} ≥ {alarm_threshold} — possible duplicate")
        if ds.max_5gram >= alarm_threshold:
            alarms.append(f"  ⚠ {ds.day}: max intra-day 5-gram={ds.max_5gram:.3f} ≥ {alarm_threshold} — possible phrasing template")
        if ds.mean_tfidf >= alarm_threshold * 0.6:
            alarms.append(f"  ⚠ {ds.day}: mean intra-day TFIDF={ds.mean_tfidf:.3f} — broad template collapse")
    if alarms:
        lines.append("  ALARMS")
        lines.append("  " + "─" * 76)
        lines.extend(alarms)
        lines.append("")

    # cross-day drift
    if extras.get("cross_day"):
        lines.append("  CROSS-DAY DRIFT  (higher mean = more recurrence; higher JS/KS = more drift)")
        lines.append("  " + "─" * 76)
        lines.append(f"  {'days':<24} {'mean(TF)':>9} {'max(TF)':>8} {'skills_JS':>10} {'diff_KS':>9}")
        for cd in extras["cross_day"]:
            lines.append(
                f"  {cd['from']} → {cd['to']:<11} "
                f"{cd['mean_tfidf']:>9.3f} {cd['max_tfidf']:>8.3f} "
                f"{cd['skills_js']:>10.3f} {cd['difficulty_ks']:>9.3f}"
            )
        lines.append("")

    # skill heatmap
    all_skills = sorted({s for ds in day_stats for s in ds.skill_counts}, key=lambda s: -sum(d.skill_counts.get(s, 0) for d in day_stats))
    if all_skills:
        lines.append("  SKILL DISTRIBUTION  (top 12 by total count)")
        lines.append("  " + "─" * 76)
        hdr = f"  {'skill':<22} " + " ".join(f"{ds.day[-5:]:>6}" for ds in day_stats) + "   bar (total)"
        lines.append(hdr)
        max_total = max(sum(ds.skill_counts.get(s, 0) for ds in day_stats) for s in all_skills) or 1
        for s in all_skills[:12]:
            counts = [ds.skill_counts.get(s, 0) for ds in day_stats]
            total = sum(counts)
            lines.append(f"  {s:<22} " + " ".join(f"{c:>6}" for c in counts) + f"   {bar(total, 20, max_total)} {total}")
        lines.append("")

    # difficulty histogram
    lines.append("  DIFFICULTY DISTRIBUTION")
    lines.append("  " + "─" * 76)
    bins = [600, 800, 1000, 1200, 1400, 1600, 2000]
    bin_lbls = [f"<{bins[0]}"] + [f"{bins[i]}-{bins[i+1]-1}" for i in range(len(bins)-1)] + [f"≥{bins[-1]}"]
    lines.append(f"  {'date':<12} " + " ".join(f"{lbl:>9}" for lbl in bin_lbls))
    for ds in day_stats:
        # we don't store individual difficulties; recompute from extras' novelty ranking would be wrong.
        # Use ds.difficulty_mean as approx; expand below by reading skill_counts not enough.
        # For correctness fall through using whole-problems via extras['_problems'] if present.
        pass
    # Better: rebuild from extras['_problems_by_day']
    by_day = extras.get("_problems_by_day", {})
    for ds in day_stats:
        diffs = by_day.get(ds.day, [])
        bin_counts = [0] * (len(bins) + 1)
        for d in diffs:
            placed = False
            for i, b in enumerate(bins):
                if d < b:
                    bin_counts[i] += 1
                    placed = True
                    break
            if not placed:
                bin_counts[-1] += 1
        lines.append(f"  {ds.day:<12} " + " ".join(f"{c:>9}" for c in bin_counts))
    lines.append("")

    # top-K nearest pairs
    sorted_pairs = sorted(pairs, key=lambda p: -p.composite)
    lines.append(f"  TOP-{top_k} NEAREST PAIRS  (composite = 0.55·TF + 0.35·5gram + 0.10·skills)")
    lines.append("  " + "─" * 76)
    lines.append(f"  {'rank':>4} {'comp':>6} {'TF':>6} {'5g':>6} {'sk':>6}  pair")
    for i, p in enumerate(sorted_pairs[:top_k], 1):
        tag = "(SAME)" if p.same_day else "(CROSS)"
        lines.append(
            f"  {i:>4} {p.composite:>6.3f} {p.tfidf:>6.3f} {p.fivegram:>6.3f} {p.skill_overlap:>6.3f}  "
            f"{p.a_key} ↔ {p.b_key} {tag}"
        )
    lines.append("")

    # novelty extremes
    nov = extras.get("novelty_ranking", [])
    if nov:
        lines.append("  LEAST NOVEL  (most template-y — these look like everything else)")
        lines.append("  " + "─" * 76)
        for r in nov[:3]:
            lines.append(f"  {r['novelty']:.3f}  {r['key']:<28}  {r['prompt_head']!r}")
        lines.append("")
        lines.append("  MOST NOVEL  (outliers — possibly creative spikes, possibly malformed)")
        lines.append("  " + "─" * 76)
        for r in nov[-3:][::-1]:
            lines.append(f"  {r['novelty']:.3f}  {r['key']:<28}  {r['prompt_head']!r}")
        lines.append("")

    # verdict
    worst_mean = max(d.mean_tfidf for d in day_stats) if day_stats else 0.0
    worst_max = max(d.max_tfidf for d in day_stats) if day_stats else 0.0
    if worst_mean >= alarm_threshold * 0.6:
        verdict = "TEMPLATE-COLLAPSING — generator producing systematically similar problems"
    elif worst_max >= alarm_threshold:
        verdict = "ISOLATED DUPLICATES — overall diverse but specific pairs warrant review"
    else:
        verdict = "HEALTHY — diversity within expected envelope"
    lines.append("─" * 78)
    lines.append(f"  Verdict: {verdict}")
    lines.append(f"  Worst intra-day mean TFIDF: {worst_mean:.3f}    Worst max: {worst_max:.3f}    Threshold: {alarm_threshold}")
    lines.append("─" * 78)

    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="autobench/benchmarks/curriculum",
                    help="curriculum directory containing <YYYY-MM-DD>/cases.jsonl subdirs")
    ap.add_argument("--top-k", type=int, default=8, help="show top-K nearest problem pairs")
    ap.add_argument("--alarm-threshold", type=float, default=0.5,
                    help="similarity >= this triggers a duplicate alarm")
    ap.add_argument("--json", help="write structured report to this path (default: tools/diversity_drift_<date>.json)")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"ERROR: {root} does not exist", file=sys.stderr)
        return 2

    problems = load_all(root)
    if len(problems) < 2:
        print(f"ERROR: found {len(problems)} problems; need at least 2", file=sys.stderr)
        return 2

    day_stats, pairs, extras = analyze(problems)
    # patch in difficulties-by-day for the histogram (avoids re-loading)
    extras["_problems_by_day"] = {}
    for p in problems:
        extras["_problems_by_day"].setdefault(p.day, []).append(p.difficulty)

    print(render(day_stats, pairs, extras, args.alarm_threshold, args.top_k))

    # structured output
    from datetime import datetime
    out_path = Path(args.json) if args.json else Path(f"tools/diversity_drift_{datetime.now().strftime('%Y-%m-%d')}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_problems": len(problems),
        "alarm_threshold": args.alarm_threshold,
        "day_stats": [asdict(d) for d in day_stats],
        "cross_day": extras.get("cross_day", []),
        "novelty_ranking": extras.get("novelty_ranking", []),
        "top_pairs": [
            {**asdict(p), "composite": p.composite}
            for p in sorted(pairs, key=lambda p: -p.composite)[:args.top_k * 3]
        ],
    }, indent=2))
    print(f"\n  → JSON: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
