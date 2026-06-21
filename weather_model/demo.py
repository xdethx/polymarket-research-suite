"""
demo.py -- W0 London worked example, no network required.

Feeds a single ECMWF ensemble forecast for London (June 10, mu=15.90 C,
sigma=0.92 C) through the complete weather-edge pipeline:

    blend_models()        ->  blended (mu, sigma)    [single model -> identity]
    make_ladder()         ->  bucket objects
    bucket_distribution() ->  model P(bucket)
    devig()               ->  de-vigged market prices
    yes_edge()            ->  edge per bucket
    kelly_size()          ->  USDC position size

Outputs an aligned table showing each step's numbers for the 13-23 C ladder.

Run from the repo root:
    python -m weather_model.demo
"""

from __future__ import annotations

from weather_model.blend import blend_models
from weather_model.bucket_model import (
    ROUNDING_WU,
    Bucket,
    bucket_distribution,
    make_ladder,
)
from weather_model.edge import (
    DEFAULT_KELLY_FRACTION,
    devig,
    kelly_size,
    min_edge_gate,
    yes_edge,
)

# ---------------------------------------------------------------------------
# Input parameters (W0 London June 10 worked example)
# ---------------------------------------------------------------------------

# ECMWF ensemble forecast for London Heathrow (EGLL), degrees Celsius.
# Single-model case: blend_models returns the same (mu, sigma) unchanged.
FORECASTS = [(15.90, 0.92)]  # (mu, sigma)

# Temperature ladder for London WU markets (C, integer steps).
# Min = lower-tail bucket ("<=13 C"), Max = upper-tail bucket (">=23 C").
LADDER_TEMPS = list(range(13, 24))  # [13, 14, ..., 23]

# Raw market YES prices from the CLOB (before de-vigging).
# Plausible W0 London mid-prices; sum ~= 1.040 (4% overround).
RAW_PRICES = {
    13: 0.010,
    14: 0.068,
    15: 0.290,
    16: 0.395,
    17: 0.175,
    18: 0.050,
    19: 0.025,
    20: 0.012,
    21: 0.007,
    22: 0.005,
    23: 0.003,
}

# Position-sizing parameters
BANKROLL_USDC = 500.0
KELLY_FRACTION = DEFAULT_KELLY_FRACTION   # 0.25 (quarter-Kelly)
MIN_EDGE = 0.03                           # signal gate: >=3pp edge required


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    # Step 1: blend forecasts -> (mu_blend, sigma_blend)
    mu_blend, sigma_blend = blend_models(FORECASTS)

    # Step 2: build bucket ladder
    ladder = make_ladder(LADDER_TEMPS)

    # Step 3: model probability for each bucket
    model_dist = bucket_distribution(ladder, mu=mu_blend, sigma=sigma_blend,
                                     regime=ROUNDING_WU)

    # Step 4: de-vig the raw market prices
    raw_list = [RAW_PRICES[b.value] for b in ladder]
    devigged = devig(raw_list)

    # Step 5: compute edge and Kelly size for each bucket (YES side)
    print()
    print(f"W0 London worked example -- ECMWF mu={mu_blend:.2f}C  sigma={sigma_blend:.2f}C")
    print(f"Bankroll ${BANKROLL_USDC:.0f}  |  Kelly fraction {KELLY_FRACTION:.0%}  "
          f"|  Min edge {MIN_EDGE:.0%}")
    print()
    header = (
        f"{'Bucket':>12}  {'Model P':>8}  {'Raw mkt':>8}  "
        f"{'Devigged':>9}  {'YES edge':>9}  {'Gate':>5}  {'Kelly $':>9}"
    )
    print(header)
    print("-" * len(header))

    for b, devig_price in zip(ladder, devigged):
        model_p = model_dist[b]
        raw_p = RAW_PRICES[b.value]
        edge = yes_edge(model_p, devig_price)
        gate = min_edge_gate(edge, MIN_EDGE)
        kelly = kelly_size(model_p, devig_price, KELLY_FRACTION, BANKROLL_USDC,
                           side="YES")

        # Label: tail buckets get inequality prefix
        if b.is_lower_tail:
            label = f"<={b.value}C"
        elif b.is_upper_tail:
            label = f">={b.value}C"
        else:
            label = f"{b.value}C"

        gate_str = "YES" if gate else "---"
        print(
            f"{label:>12}  {model_p:>8.4f}  {raw_p:>8.3f}  "
            f"{devig_price:>9.4f}  {edge:>+9.4f}  {gate_str:>5}  "
            f"{kelly:>8.2f}$"
        )

    print()
    print(f"Overround: {sum(raw_list):.3f} -> de-vigged sum = {sum(devigged):.6f}")
    print()


if __name__ == "__main__":
    main()
