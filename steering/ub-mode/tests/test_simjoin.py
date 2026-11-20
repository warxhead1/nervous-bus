"""simjoin must return exactly the pairs the brute-force loop returns."""

import importlib.util
import random
from collections import namedtuple
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
spec = importlib.util.spec_from_file_location("simjoin", BIN / "simjoin.py")
simjoin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simjoin)

E = namedtuple("E", "language fingerprints")


def jaccard(a, b):
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def brute(entries, t, probe=None):
    return sorted(simjoin._brute(entries, t, jaccard, probe))


def corpus(seed, n=300):
    rng = random.Random(seed)
    bases = [frozenset(rng.sample(range(400), rng.randint(5, 60))) for _ in range(25)]
    out = []
    for _ in range(n):
        base = set(rng.choice(bases))
        for _ in range(rng.randint(0, 6)):
            if base and rng.random() < 0.5:
                base.discard(rng.choice(sorted(base)))
            else:
                base.add(rng.randrange(400))
        out.append(E(rng.choice(["go", "python"]), frozenset(base)))
    out.append(E("go", frozenset()))
    return out


def test_matches_brute_force_across_thresholds():
    for seed in range(6):
        entries = corpus(seed)
        for t in (0.5, 0.8, 0.82, 0.9, 1.0):
            want = brute(entries, t)
            got = sorted(simjoin.similar_pairs(entries, t, jaccard))
            assert got == want, (seed, t)
            if t <= 0.82:
                assert want, "corpus too sparse to exercise the join"


def test_probe_keeps_only_pairs_touching_changed_entries():
    entries = corpus(7)
    probe = set(range(0, len(entries), 9))
    want = brute(entries, 0.82, probe)
    got = sorted(simjoin.similar_pairs(entries, 0.82, jaccard, probe))
    assert got == want and want
    assert all(i in probe or j in probe for i, j, _ in got)


def test_languages_are_never_paired():
    same = frozenset(range(30))
    entries = [E("go", same), E("python", same)]
    assert list(simjoin.similar_pairs(entries, 0.82, jaccard)) == []

