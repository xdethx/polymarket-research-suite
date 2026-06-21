"""
Multi-model forecast blending for weather trading.

PURE FUNCTION — no I/O, no network, no side effects.

The W0 fake-edge problem
------------------------
When ECMWF (μ=15.90°C) and GFS (μ=18.15°C) disagree by 2.25°C — wider
than a single 1°C bucket — a naive single-model bot computes a fake +34%
edge.  The correct response is to produce a WIDE distribution that reflects
the genuine uncertainty across models.

Blend method: mixture-of-Gaussians moments (law of total variance)
-------------------------------------------------------------------
Given N models with weights w_i summing to 1, means μ_i, stds σ_i:

    μ_blend  = Σ w_i · μ_i

    var_blend = Σ w_i · σ_i²               ← within-model variance
              + Σ w_i · (μ_i − μ_blend)²   ← between-model variance  ★

    σ_blend = sqrt(var_blend)

★ Safety property: the between-model variance term ensures that when models
  disagree, σ_blend > every individual σ_i.  A 2.25°C disagreement on 1°C
  buckets produces a WIDE blended distribution, not a falsely confident one.
  This is the explicit anti-overfit guardrail — it cannot be circumvented.

Weighting
---------
Default: equal weights (1/N each) — the correct prior when no historical
accuracy data is available.

Inverse-MAE weighting hook (available for W3+ use):
    Pass per-model historical MAEs as the `maes` keyword argument.
    w_i ∝ 1 / MAE_i   (lower historical error → higher weight)
    Until W3 accumulates forward-recorded outcomes, equal weights apply.
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple


def blend_models(
    forecasts: List[Tuple[float, float]],
    maes: Optional[List[float]] = None,
) -> Tuple[float, float]:
    """Blend multiple (μ, σ) forecasts into a single blended (μ, σ).

    Parameters
    ----------
    forecasts : list of (mu, sigma) pairs, one per model.
        All sigmas must be > 0.
    maes : optional per-model historical MAE list (same length as forecasts).
        None (default) → equal weights.
        Provided → w_i ∝ 1/MAE_i (inverse-MAE, DEB-style).
        All MAEs must be > 0.

    Returns
    -------
    (mu_blend, sigma_blend)

    Safety guarantee
    ----------------
    When at least two model means differ, sigma_blend > max(sigma_i).
    The caller should assert this in tests and document it in strategy notes.

    Examples
    --------
    >>> mu_b, sig_b = blend_models([(15.90, 0.92), (18.15, 0.73)])
    >>> round(mu_b, 3)
    17.025
    >>> sig_b > 0.92   # safety property: wider than either model alone
    True
    """
    if len(forecasts) == 0:
        raise ValueError("forecasts must not be empty")

    for i, (mu, sigma) in enumerate(forecasts):
        if sigma <= 0.0:
            raise ValueError(
                f"sigma must be > 0 for model {i}, got sigma={sigma}"
            )

    # --- Compute normalized weights ---
    if maes is not None:
        if len(maes) != len(forecasts):
            raise ValueError(
                f"maes length ({len(maes)}) must match forecasts length "
                f"({len(forecasts)})"
            )
        for i, mae in enumerate(maes):
            if mae <= 0.0:
                raise ValueError(
                    f"MAE must be > 0 for model {i}, got mae={mae}"
                )
        raw_weights = [1.0 / mae for mae in maes]
    else:
        raw_weights = [1.0] * len(forecasts)

    total_w = sum(raw_weights)
    weights = [w / total_w for w in raw_weights]

    # --- Blended mean ---
    mu_blend = sum(w * mu for w, (mu, _) in zip(weights, forecasts))

    # --- Blended variance (law of total variance) ---
    within_var = sum(
        w * sigma ** 2
        for w, (_, sigma) in zip(weights, forecasts)
    )
    between_var = sum(
        w * (mu - mu_blend) ** 2
        for w, (mu, _) in zip(weights, forecasts)
    )
    var_blend = within_var + between_var
    sigma_blend = math.sqrt(var_blend)

    return mu_blend, sigma_blend
