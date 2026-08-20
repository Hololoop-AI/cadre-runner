"""Small-n statistics for eval trials. Hand-written on purpose: no framework
ships small-n-correct math, and CLT/normal intervals are dangerously narrow
under a few hundred datapoints (arXiv:2503.01747). Stdlib only."""

import math


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion — honest at the k=3..10
    trial counts eval runs can afford, where a normal approximation lies."""
    if trials == 0:
        return (0.0, 1.0)
    if not 0 <= successes <= trials:
        raise ValueError(f"successes {successes} outside [0, {trials}]")
    p = successes / trials
    denom = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return (max(0.0, center - half), min(1.0, center + half))


def pass_power_k(successes: int, trials: int, k: int) -> float:
    """P(all k independent attempts succeed) ≈ (c/n)^k — the reliability
    metric. pass@k answers "could it ever"; a pipeline needs "does it,
    every time"."""
    if trials == 0:
        return 0.0
    return (successes / trials) ** k


def brier(pairs: list[tuple[float, bool]]) -> float:
    """Mean squared error of stated probability vs outcome. Confidence
    verdicts map to probabilities upstream (e.g. high/medium/low -> observed
    historical rates, or a fixed prior until history exists)."""
    if not pairs:
        return 0.0
    return sum((p - (1.0 if ok else 0.0)) ** 2 for p, ok in pairs) / len(pairs)


def expected_calibration_error(pairs: list[tuple[float, bool]], bins: int = 5) -> float:
    """ECE: confidence-bucket |accuracy − mean confidence|, weighted by bucket
    size. With few samples use few bins; an empty history returns 0."""
    if not pairs:
        return 0.0
    buckets: dict[int, list[tuple[float, bool]]] = {}
    for p, ok in pairs:
        buckets.setdefault(min(int(p * bins), bins - 1), []).append((p, ok))
    n = len(pairs)
    ece = 0.0
    for items in buckets.values():
        acc = sum(1 for _, ok in items if ok) / len(items)
        conf = sum(p for p, _ in items) / len(items)
        ece += (len(items) / n) * abs(acc - conf)
    return ece
