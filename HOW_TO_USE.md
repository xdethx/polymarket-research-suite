# How to Use

Practical companion to [README.md](README.md) (what & why → there; how to run → here).

---

## 1. Setup

Python 3.10+.

```bash
pip install -r requirements.txt
```

`requests`, `websocket-client`, and `pytest` — nothing else. The weather model's Gaussian
CDF is implemented via stdlib `math.erf`; no numpy, scipy, or pandas anywhere.

---

## 2. Quick start — zero network required

```bash
python -m weather_model.demo
```

Runs the complete weather-edge pipeline (blend → bucket probabilities → de-vig → edge →
Kelly size) on a hard-coded W0 London worked example (ECMWF ensemble, μ=15.90°C σ=0.92°C).
No network, no API keys. Output: an aligned table of all 11 temperature buckets with model
probability, raw market price, de-vigged price, YES edge, gate pass/fail, and Kelly-sized
USDC position.

---

## 3. Run the tests

```bash
python -m pytest weather_model/tests -q
```

244 passed, ~0.4s. Pure-logic unit tests — no network calls, no fixtures that expire. Tests
cover: Gaussian CDF properties, both rounding regimes (WU and HKO), the W0 London
worked-example acceptance check, blend safety property (σ\_blend > max(σᵢ) when models
disagree), Kelly formula derivation, and city-registry ICAO correctness.

---

## 4. Weather pipeline — live, read-only

Discovers today's open temperature markets on Polymarket, fetches ensemble forecasts from
Open-Meteo, and computes edge per bucket. All endpoints are public and keyless.

```python
from datetime import date
from weather_model import gamma_discovery, forecast_fetcher, blend, bucket_model, edge, prices_history

# 1. Discover open markets for London (today + tomorrow)
markets = gamma_discovery.discover_markets(city_slugs=["london"], horizon_days=1)
if not markets:
    print("No London temperature markets open today.")
    raise SystemExit

mkt = markets[0]
lat, lon = mkt.coords           # resolution-station coordinates (EGLL)
date_str = mkt.date             # e.g. "2026-06-21"

# 2. Fetch ECMWF + GFS ensemble forecasts
ecmwf_members = forecast_fetcher.fetch_ensemble(lat, lon, date_str, model="ecmwf_ifs025", unit=mkt.unit)
gfs_members   = forecast_fetcher.fetch_ensemble(lat, lon, date_str, model="gfs_seamless",  unit=mkt.unit)

mu_ecmwf, s_ecmwf = forecast_fetcher.members_to_mu_sigma(ecmwf_members)
mu_gfs,   s_gfs   = forecast_fetcher.members_to_mu_sigma(gfs_members)

# 3. Blend models (law-of-total-variance anti-overfit guardrail)
mu_b, sigma_b = blend.blend_models([(mu_ecmwf, s_ecmwf), (mu_gfs, s_gfs)])

# 4. Build bucket ladder + model probabilities
ladder   = bucket_model.make_ladder([b.value for b in mkt.buckets if b.model_compatible])
model_dist = bucket_model.bucket_distribution(ladder, mu_b, sigma_b, mkt.rounding_regime)

# 5. Edge per bucket
for b in ladder:
    token_id    = next(bk.yes_token_id for bk in mkt.buckets if bk.value == b.value)
    market_yes  = prices_history.latest_price(token_id)   # live CLOB price
    if market_yes is None:
        continue
    raw_prices  = [prices_history.latest_price(bk.yes_token_id) or 0 for bk in mkt.buckets if bk.model_compatible]
    devigged    = edge.devig(raw_prices)
    model_p     = model_dist[b]
    e           = edge.yes_edge(model_p, devigged[ladder.index(b)])
    kelly       = edge.kelly_size(model_p, devigged[ladder.index(b)], 0.25, 500.0, "YES")
    print(f"{b.value}°{mkt.unit}: model={model_p:.3f}  edge={e:+.3f}  Kelly=${kelly:.2f}")
```

> The `demo.py` quick-start above shows the same pipeline with hard-coded values and
> prettier output. Use that to understand the pipeline before running live.

---

## 5. Data-ingestion recorder

Connects to the **public** Polymarket WebSocket feed and Binance REST API — no
authentication, no trading. Records one JSONL session file per symbol per day.

The five modules use bare imports (`from data_bus import DataBus`), so run from inside
`data_ingestion/`:

```bash
cd data_ingestion
python recorder.py --symbols BTC ETH SOL XRP --intervals 5 15
```

Press `Ctrl+C` to stop. Session records are written to `logs/market_data/{symbol}_{date}.jsonl`
at session close. A single-instance lock (`logs/.recorder.lock`) prevents two processes from
writing the same files simultaneously.

**Flags:**
- `--symbols` — space-separated list (default: BTC ETH SOL XRP DOGE BNB)
- `--intervals` — session lengths in minutes (default: 5 15)
- `--log-dir` — output directory (default: `logs/market_data`)

**Session JSONL schema (brief):** `slug`, `symbol`, `interval`, `ptb` (Binance open price),
`open_volatility`, `open_momentum_5m`, `close_price`, `actual_outcome` (`"UP"` / `"DOWN"`),
`snapshots[]` (per-tick: `t` seconds-remaining, `mid`, `spread`, `imbalance`,
`binance_price`, `momentum_3m`, `momentum_5m`). Full schema: `data_ingestion/README.md`.

> Read-only data capture only. Order execution, signing, and authentication are not
> present in this module and are not shipped in this repository.

---

## 6. Discovery-engine predictor screen

Screens open-tick features against the market-error residual to test whether any signal
predicts where the Polymarket mid is wrong relative to the final binary outcome.

```bash
python discovery_engine/screen_predictors.py
```

Expects `discovery_engine/sessions_leadlag.csv` — **illustrative**: run against your own
market-session data. The script exits with a clear error message if the file is absent.
Required column schema: `discovery_engine/README.md`.

Output: structured ENTRY (MARKET EFFICIENT / SIGNAL FOUND) and EXIT (NO EARLY-EXIT
SIGNAL / SIGNAL FOUND) verdicts with supporting statistics.
