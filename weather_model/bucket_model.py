"""
Bucket probability model for weather trading.

PURE FUNCTION — no I/O, no network, no side effects.

Given a forecast distribution (mean μ, std σ) and a market bucket ladder,
computes P(bucket) for each bucket via the normal CDF, branching on the
city's rounding regime.

Rounding regimes
----------------
wu_round_half_up  (all WU cities — London, Paris, NY, etc.):
    Measured daily high reported to nearest whole degree.
    Bucket N covers the temperature interval [N-0.5, N+0.5).
    P = Φ(N+0.5; μ,σ) − Φ(N-0.5; μ,σ)

hko_floor  (Hong Kong only):
    HKO reports to one decimal place; Polymarket then applies floor.
    Bucket N covers the interval [N, N+1).
    P = Φ(N+1; μ,σ) − Φ(N; μ,σ)

Open-ended end buckets
----------------------
Lower tail "N or below":
    wu_round  → P = Φ(N+0.5; μ,σ)   (all measured values that round to ≤ N)
    hko_floor → P = Φ(N+1; μ,σ)     (all values whose floor is ≤ N)

Upper tail "N or higher":
    wu_round  → P = 1 − Φ(N-0.5; μ,σ)
    hko_floor → P = 1 − Φ(N; μ,σ)

W1 Acceptance check
-------------------
Reproduces W0 London June 10 worked example (ECMWF ensemble, ~D-1):
    μ=15.90°C, σ=0.92°C, regime=wu_round_half_up
    → 16°C P≈0.41,  15°C P≈0.27,  17°C P≈0.22

Run as a script to print the hand-verification:
    python -m weather_model.bucket_model
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List

# Rounding regime constants (mirrors weather_model/cities.py — defined here so
# bucket_model.py is usable standalone without importing the city registry).
ROUNDING_WU = "wu_round_half_up"
ROUNDING_HKO = "hko_floor"


@dataclass(frozen=True)
class Bucket:
    """A single market temperature bucket.

    value : integer degree label (e.g. 16 for "16°C" or "16°F").
    is_lower_tail : True when this is the open-ended lower bucket ("N or below").
    is_upper_tail : True when this is the open-ended upper bucket ("N or higher").
    Interior buckets have both flags False.
    """
    value: int
    is_lower_tail: bool = False
    is_upper_tail: bool = False


# ---------------------------------------------------------------------------
# Gaussian CDF
# ---------------------------------------------------------------------------

def normal_cdf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    """Cumulative distribution function of N(mu, sigma²).

    Φ(x; μ, σ) = 0.5 * (1 + erf((x − μ) / (σ√2)))

    Degenerate case σ = 0 (point mass at mu):
        Returns 0.0 if x < mu, 0.5 if x == mu, 1.0 if x > mu.

    Raises ValueError for sigma < 0.
    """
    if sigma < 0.0:
        raise ValueError(f"sigma must be >= 0, got {sigma}")
    if sigma == 0.0:
        if x < mu:
            return 0.0
        elif x == mu:
            return 0.5
        else:
            return 1.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


# ---------------------------------------------------------------------------
# Per-bucket probability
# ---------------------------------------------------------------------------

def bucket_probability(
    bucket: Bucket, mu: float, sigma: float, regime: str
) -> float:
    """Compute P(bucket) for a single bucket given forecast N(mu, sigma) and regime.

    Returns a probability in [0.0, 1.0].
    """
    n = bucket.value

    if regime == ROUNDING_WU:
        if bucket.is_lower_tail:
            # "N or below": continuous measurement < N+0.5 rounds to ≤ N
            return normal_cdf(n + 0.5, mu, sigma)
        elif bucket.is_upper_tail:
            # "N or higher": continuous measurement ≥ N-0.5 rounds to ≥ N
            return 1.0 - normal_cdf(n - 0.5, mu, sigma)
        else:
            return normal_cdf(n + 0.5, mu, sigma) - normal_cdf(n - 0.5, mu, sigma)

    elif regime == ROUNDING_HKO:
        if bucket.is_lower_tail:
            # "N or below": floor(measurement) ≤ N → measurement < N+1
            return normal_cdf(n + 1.0, mu, sigma)
        elif bucket.is_upper_tail:
            # "N or higher": floor(measurement) ≥ N → measurement ≥ N
            return 1.0 - normal_cdf(float(n), mu, sigma)
        else:
            return normal_cdf(n + 1.0, mu, sigma) - normal_cdf(float(n), mu, sigma)

    else:
        raise ValueError(f"Unknown rounding regime: {regime!r}")


# ---------------------------------------------------------------------------
# Full-ladder distribution
# ---------------------------------------------------------------------------

def bucket_distribution(
    buckets: List[Bucket], mu: float, sigma: float, regime: str
) -> Dict[Bucket, float]:
    """Compute P(bucket) for every bucket in the ladder.

    For a complete ladder (lower-tail + all interior + upper-tail), the
    returned probabilities sum to ≈ 1.0 (within floating-point precision).

    Returns a dict mapping each Bucket to its probability.
    """
    return {b: bucket_probability(b, mu, sigma, regime) for b in buckets}


# ---------------------------------------------------------------------------
# Ladder construction helper
# ---------------------------------------------------------------------------

def make_ladder(temperatures: List[int]) -> List[Bucket]:
    """Build a standard bucket ladder from a list of integer temperatures.

    The minimum value becomes the lower tail ("T or below").
    The maximum value becomes the upper tail ("T or higher").
    All remaining values in between are interior buckets.

    The input is sorted automatically; duplicates are silently dropped.

    Example:
        make_ladder([13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23])
        → [Bucket(13, is_lower_tail=True), Bucket(14), ...,
           Bucket(22), Bucket(23, is_upper_tail=True)]
    """
    if not temperatures:
        raise ValueError("temperatures list must not be empty")
    temps = sorted(set(temperatures))
    buckets: List[Bucket] = []
    for i, t in enumerate(temps):
        if i == 0 and len(temps) == 1:
            # Degenerate single-bucket market: treat as lower tail
            buckets.append(Bucket(t, is_lower_tail=True))
        elif i == 0:
            buckets.append(Bucket(t, is_lower_tail=True))
        elif i == len(temps) - 1:
            buckets.append(Bucket(t, is_upper_tail=True))
        else:
            buckets.append(Bucket(t))
    return buckets


# ---------------------------------------------------------------------------
# __main__ — hand-verification of W0 worked example (W1 acceptance check)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from weather_model.blend import blend_models

    # --- W0 London June 10 worked example ---
    mu_ecmwf, sigma_ecmwf = 15.90, 0.92
    regime = ROUNDING_WU
    ladder = make_ladder(list(range(13, 24)))  # 13C or below … 23C or higher

    print("=" * 60)
    print("W0 acceptance check -- London June 10, ECMWF only")
    print(f"  mu={mu_ecmwf}, sigma={sigma_ecmwf}, regime={regime}")
    print("=" * 60)

    dist = bucket_distribution(ladder, mu_ecmwf, sigma_ecmwf, regime)
    total = 0.0
    for b in ladder:
        p = dist[b]
        if b.is_lower_tail:
            label = f"{b.value}C or below"
        elif b.is_upper_tail:
            label = f"{b.value}C or higher"
        else:
            label = f"{b.value}C"
        print(f"  {label:20s}  P = {p:.4f}")
        total += p
    print(f"  {'TOTAL':20s}  P = {total:.6f}")

    print()
    p16 = dist[Bucket(16)]
    p15 = dist[Bucket(15)]
    p17 = dist[Bucket(17)]
    print(f"  16C  P = {p16:.4f}   expect ~0.41   {'OK' if abs(p16-0.41)<0.01 else 'FAIL'}")
    print(f"  15C  P = {p15:.4f}   expect ~0.27   {'OK' if abs(p15-0.27)<0.01 else 'FAIL'}")
    print(f"  17C  P = {p17:.4f}   expect ~0.22   {'OK' if abs(p17-0.22)<0.01 else 'FAIL'}")

    # --- Blend safety-property check ---
    print()
    print("=" * 60)
    print("W0 blend safety check -- ECMWF+GFS disagreement -> wide sigma")
    print(f"  ECMWF: mu={mu_ecmwf}, sigma={sigma_ecmwf}")
    print(f"  GFS:   mu=18.15,    sigma=0.73")
    print("=" * 60)

    mu_b, sigma_b = blend_models([(mu_ecmwf, sigma_ecmwf), (18.15, 0.73)])
    print(f"  Blended mu = {mu_b:.4f}   (expect between 15.90 and 18.15)")
    print(f"  Blended sigma = {sigma_b:.4f}   (expect > max(0.92, 0.73) = 0.92)")
    ok_mu = 15.90 < mu_b < 18.15
    ok_sigma = sigma_b > 0.92
    print(f"  mu between models?   {'OK' if ok_mu else 'FAIL'}")
    print(f"  sigma > each model's sigma? {'OK' if ok_sigma else 'FAIL'}")
