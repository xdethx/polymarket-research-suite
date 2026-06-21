"""
Edge calculation and position sizing for weather trading.

PURE FUNCTION — no I/O, no network, no side effects.

Computes per-bucket YES/NO edge and fractional-Kelly position sizes.
No execution, no order routing — just the numbers.

Pipeline position
-----------------
bucket_model.py  →  blend.py  →  edge.py  →  [order routing, out of scope]

For each temperature bucket:
    1. bucket_model.py gives P(bucket) from the blended forecast.
    2. devig() removes the market overround from raw prices.
    3. yes_edge() / no_edge() measure the model-vs-market gap.
    4. min_edge_gate() filters noise (only trade when edge is meaningful).
    5. kelly_size() computes a fractional-Kelly position size.
"""
from __future__ import annotations

from typing import List

# Conservative default Kelly fraction.  Actual sizing must come from the preset.
# Quarter-Kelly limits the ruin probability while still capturing most EV.
DEFAULT_KELLY_FRACTION: float = 0.25


# ---------------------------------------------------------------------------
# De-vigging (overround removal)
# ---------------------------------------------------------------------------

def devig(prices: List[float]) -> List[float]:
    """Remove the overround from a raw bucket-ladder of YES prices.

    Raw prices typically sum to > 1.0 due to the market maker's overround
    (~2–4% per the W0 London example: sum ≈ 1.040).  Normalizes proportionally
    so the returned list sums to exactly 1.0.

    Parameters
    ----------
    prices : raw YES prices for each bucket, in any order.

    Returns
    -------
    True implied probabilities summing to 1.0.

    Raises
    ------
    ValueError if prices is empty or sums to zero.
    """
    if not prices:
        raise ValueError("prices must not be empty")
    total = sum(prices)
    if total <= 0.0:
        raise ValueError(f"prices must sum to a positive value, got {total}")
    return [p / total for p in prices]


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------

def yes_edge(bucket_prob: float, market_price: float) -> float:
    """YES-side edge: model probability minus market YES price.

    Positive  → model says YES is underpriced → candidate BUY YES.
    Negative  → model says YES is overpriced → no edge on YES side.

    Parameters
    ----------
    bucket_prob  : model's P(bucket resolves YES), from bucket_model.py
    market_price : market's current YES price (0 < price < 1), de-vigged
    """
    return bucket_prob - market_price


def no_edge(bucket_prob: float, market_price: float) -> float:
    """NO-side edge: market YES price minus model probability.

    Positive  → model says YES is overpriced → market underprices NO →
                candidate BUY NO.
    Negative  → model says YES is underpriced → no edge on NO side.

    Parameters
    ----------
    bucket_prob  : model's P(bucket resolves YES), from bucket_model.py
    market_price : market's current YES price (0 < price < 1), de-vigged

    Note: for a binary market, NO price = 1 − market_price.
    The NO edge is equivalent to (NO true prob) − (NO market price)
    = (1−bucket_prob) − (1−market_price) = market_price − bucket_prob.
    """
    return market_price - bucket_prob


# ---------------------------------------------------------------------------
# Fractional-Kelly position sizing
# ---------------------------------------------------------------------------

def kelly_size(
    bucket_prob: float,
    market_price: float,
    kelly_fraction: float,
    bankroll: float,
    side: str,
) -> float:
    """Fractional-Kelly position size for a single weather bucket.

    Kelly formula for a binary market
    -----------------------------------
    Let q = bucket_prob (model's true probability for the bet to win),
        p = market_price (cost per contract in USDC).

    YES side (bet wins if bucket resolves YES; cost = p per contract):
        f* = (q − p) / (1 − p)      when q > p, else 0

    NO side  (bet wins if bucket does NOT resolve YES; cost = 1−p per contract;
              equivalent to a bet on true_prob=1−q vs price=1−p):
        f* = ((1−q) − (1−p)) / (1 − (1−p)) = (p − q) / p   when p > q, else 0

    Final size:
        size_usdc = bankroll × kelly_fraction × max(f*, 0)

    Parameters
    ----------
    bucket_prob    : model's P(YES for this bucket), in (0, 1)
    market_price   : market YES price (de-vigged), in (0, 1) exclusive
    kelly_fraction : fraction of full Kelly to apply (e.g. 0.25 for quarter-Kelly)
    bankroll       : total capital available in USDC
    side           : "YES" or "NO" (case-insensitive)

    Returns
    -------
    Position size in USDC, ≥ 0.  Returns 0 when there is no edge.

    Raises
    ------
    ValueError for invalid inputs.
    """
    if not (0.0 < market_price < 1.0):
        raise ValueError(
            f"market_price must be in (0, 1) exclusive, got {market_price}"
        )
    if kelly_fraction <= 0.0:
        raise ValueError(
            f"kelly_fraction must be > 0, got {kelly_fraction}"
        )
    if bankroll < 0.0:
        raise ValueError(f"bankroll must be >= 0, got {bankroll}")

    side_upper = side.strip().upper()
    if side_upper == "YES":
        f_star = (bucket_prob - market_price) / (1.0 - market_price)
    elif side_upper == "NO":
        f_star = (market_price - bucket_prob) / market_price
    else:
        raise ValueError(f"side must be 'YES' or 'NO', got {side!r}")

    f_star = max(f_star, 0.0)
    return bankroll * kelly_fraction * f_star


# ---------------------------------------------------------------------------
# Minimum-edge gate
# ---------------------------------------------------------------------------

def min_edge_gate(edge_value: float, threshold: float) -> bool:
    """Return True only when |edge_value| >= threshold.

    Filters noise — only signals with an edge large enough to be meaningful
    clear the gate.  Use for both YES and NO edges (absolute value check).

    Parameters
    ----------
    edge_value : output of yes_edge() or no_edge()
    threshold  : minimum required |edge|, e.g. 0.05 for 5 percentage points

    Returns
    -------
    True if |edge_value| >= threshold, False otherwise.
    """
    return abs(edge_value) >= threshold
