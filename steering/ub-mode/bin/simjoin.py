"""Exact Jaccard similarity join with prefix filtering.

dryer compares every pair of forms in a language, which is quadratic: about
25k Go functions take hours. This returns the same pairs as that brute-force
loop, but only scores pairs that can reach the threshold:

- Order each language's fingerprints rarest first.
- If J(A, B) >= t then |A & B| >= t * max(|A|, |B|), so the first
  |A| - ceil(t * |A|) + 1 fingerprints of A and of B must share one.
- |B| must lie within [t * |A|, |A| / t].

Every surviving pair is scored with the caller's jaccard function and the
caller's `score < threshold` test, so the result is exact, not approximate.
Pair orientation (left = earlier entry) matches the brute-force loop.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

EPS = 1e-9


def _prefix_len(size: int, threshold: float) -> int:
    need = math.ceil(threshold * size - EPS)
    return max(1, min(size, size - need + 1))


def similar_pairs(entries, threshold, jaccard, probe=None):
    """Yield (i, j, score) with i < j, same language, score >= threshold.

    entries: objects with .language and .fingerprints (a frozenset).
    probe: optional set of indexes. When given, a pair is kept only if at
    least one side is in it (new code checked against the whole corpus).
    """
    if threshold <= 0:
        yield from _brute(entries, threshold, jaccard, probe)
        return
    by_language = defaultdict(list)
    for index, entry in enumerate(entries):
        by_language[entry.language].append(index)
    for indexes in by_language.values():
        yield from _join(entries, indexes, threshold, jaccard, probe)


def _join(entries, indexes, threshold, jaccard, probe):
    freq = Counter()
    for i in indexes:
        freq.update(entries[i].fingerprints)
    order = {token: rank for rank, (token, _) in enumerate(sorted(freq.items(), key=lambda kv: (kv[1], kv[0])))}
    postings = defaultdict(list)
    for i in indexes:
        tokens = entries[i].fingerprints
        if not tokens:
            continue  # an empty set scores 0.0, so it never clears t > 0
        ranked = sorted(tokens, key=order.__getitem__)
        for token in ranked[: _prefix_len(len(ranked), threshold)]:
            postings[token].append(i)
    seen = set()
    for members in postings.values():
        for a_pos, a in enumerate(members):
            size_a = len(entries[a].fingerprints)
            for b in members[a_pos + 1 :]:
                i, j = (a, b) if a < b else (b, a)
                if (i, j) in seen:
                    continue
                size_b = len(entries[b].fingerprints)
                small, large = min(size_a, size_b), max(size_a, size_b)
                if small < threshold * large - EPS:
                    continue
                seen.add((i, j))
                if probe is not None and i not in probe and j not in probe:
                    continue
                score = jaccard(entries[i].fingerprints, entries[j].fingerprints)
                if score < threshold:
                    continue
                yield i, j, score


def _brute(entries, threshold, jaccard, probe):
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            if entries[i].language != entries[j].language:
                continue
            if probe is not None and i not in probe and j not in probe:
                continue
            score = jaccard(entries[i].fingerprints, entries[j].fingerprints)
            if score >= threshold:
                yield i, j, score
