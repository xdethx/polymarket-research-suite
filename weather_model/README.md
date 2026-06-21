# weather_model

A pure-function pipeline for computing whether an ensemble weather forecast identifies
mispriced Polymarket daily-temperature buckets.

## What this demonstrates

This package implements the complete decision path from raw forecast parameters to a
signed edge value and Kelly-sized position, with no I/O anywhere in the core logic:

```
cities.py         city → ICAO station → unit, rounding regime
     ↓
bucket_model.py   (μ, σ) + market ladder → P(bucket) via Gaussian CDF
     ↓
blend.py          N model forecasts → blended (μ_blend, σ_blend)   [anti-overfit guardrail]
     ↓
edge.py           blended P(bucket) − de-vigged market price → edge, Kelly size
     ↓
station_obs.py    METAR/ASOS realized-high client (verification only, not trading input)
```

The four I/O adapters wrap the pure core for live use:

```
forecast_fetcher.py   Open-Meteo ECMWF ensemble HTTP fetch → list of (mu, sigma) per member
gamma_discovery.py    Polymarket Gamma API → active temperature-bucket event metadata
prices_history.py     Polymarket CLOB prices-history endpoint → token price time-series
clob_book.py          Polymarket CLOB order-book snapshot → best bid/ask per bucket token
```

All I/O modules are network-only adapters; they return plain dicts/lists that the pure
core functions accept directly.

## Quick-start

```bash
# See the full pipeline in action with the W0 London worked example (no network)
python -m weather_model.demo

# Run all unit tests
pip install pytest requests
pytest weather_model/tests -q
```

## Key design properties

**Gaussian CDF via `math.erf` only.** No numpy, no scipy — `normal_cdf` is
implemented as `0.5 * (1 + erf((x − μ) / (σ√2)))` using the Python stdlib.

**Anti-overfit guardrail in `blend.py`.** When two ensemble models disagree,
`blend_models()` produces a blended σ that is *wider* than either individual model's σ.
This is guaranteed by the law-of-total-variance: the between-model variance term
`Σ wᵢ·(μᵢ − μ_blend)²` is always non-negative, and is strictly positive when means differ.
A 2.25°C inter-model spread on a 1°C-wide bucket ladder inflates σ from 0.92 to 1.40,
collapsing the apparent "edge" to near-zero — the correct response.

**Two rounding regimes.** WU markets round measured highs to the nearest integer
(`bucket N covers [N−0.5, N+0.5)`). HKO markets (Hong Kong) apply floor
(`bucket N covers [N, N+1)`). Both are handled in `bucket_model.py`; the regime is
read from the city registry so callers never branch on it explicitly.

**Resolution-station coordinates.** `cities.py` stores ICAO coordinates of the
*resolution station*, not city-center coordinates. Using city-center coordinates
introduces systematic forecast error of 3–8°F (confirmed during W0 research).

**Ground truth: METAR/ASOS only.** `station_obs.py` fetches from
aviationweather.gov (AWC) and IEM ASOS archive — no-auth, public APIs.
The W2.6 research phase found that Open-Meteo reanalysis underestimates
station temperature by 0.9–4.4°C on 1°C-wide markets, causing bucket-level
errors. See `../findings/weather-metar-lesson.md`.

## Running the tests

```bash
# From the repo root
pip install pytest requests
pytest weather_model/tests -q
# -> 244 passed
```

All 244 tests are pure-logic unit tests (no network calls). They cover:
- `normal_cdf`: standard quantiles, symmetry, σ=0 degenerate, negative σ rejection
- `bucket_probability`: W0 London worked example (16°C P≈0.41, 15°C P≈0.27, 17°C P≈0.22),
  both WU and HKO regimes, tail and interior formulae
- `blend_models`: safety property (σ_blend > max(σᵢ) when means differ), law-of-total-variance
  numerical check, inverse-MAE weighting, identical-model identity
- `kelly_size`: YES/NO formula derivation, monotonicity, bankroll/fraction linearity,
  case-insensitivity, boundary validation
- `get_city`/`all_cities`: ICAO correctness (especially Paris LFPB ≠ CDG LFPG),
  rounding-regime assignment, coordinate ranges

## Dependencies

```
stdlib: math, dataclasses, typing, datetime
third-party: requests  (station_obs.py only)
```
