# Finding: Open-Meteo Reanalysis Is Not a Valid Ground-Truth Source for Polymarket Weather Markets

During the W2.5 and W2.6 phases of the weather forecast-arbitrage investigation, a
measurement error was identified that invalidated what initially appeared to be confirmed
edge on three East Asian markets. The error originated from using a model reanalysis
product (Open-Meteo `forecast+past_days`) as a proxy for realized station temperature.

---

## Background

Polymarket daily-temperature markets resolve on the official weather-station reading for a
named city (e.g. Tokyo Haneda RJTT, Shanghai Pudong ZSPD, Incheon Airport RKSI).
The resolution source is typically Weather Underground (WU), which reports the station's
measured daily maximum to the nearest whole degree under a round-half-up rule.

An edge exists if an ensemble weather model's probability distribution for the temperature
ladder differs materially from the market's implied distribution *and* the model is correct.
Verifying which source is correct requires an independent, authoritative realized temperature —
specifically, the **METAR/ASOS** observation from the resolution station itself.

---

## The error

During the W2.5 validation phase, realized daily highs were sourced from
**Open-Meteo `forecast+past_days=2`** (a model reanalysis product) instead of from station
observations. This product blends model output with assimilation data and can differ
substantially from the actual surface measurement at a specific airport station.

Results for three cities where the model-based analysis reported "EDGE IS REAL":

| City (ICAO) | Open-Meteo "realized" | METAR/ASOS realized | Δ (METAR − OM) | Market bucket | OM bucket |
|---|---|---|---|---|---|
| Tokyo (RJTT) | 20.2°C | 22.0°C | **+1.8°C** | 22°C | 20°C |
| Seoul (RKSI) | 20.6°C | 25.0°C | **+4.4°C** | 25°C | 21°C |
| Shanghai (ZSPD) | 24.1°C | 25.0°C | **+0.9°C** | 25°C | 24°C |

In every case:
- Open-Meteo underestimated the station temperature by 0.9–4.4°C
- The model's predicted bucket matched the (incorrect) Open-Meteo "realized" value
- The market's modal bucket matched the METAR/ASOS realized value
- Reclassifying with METAR data: **0 of 3 cases showed genuine edge; all 3 were OUR-ERROR**

The **W2.5 verdict** ("EDGE IS REAL — 3/5 cities") was **retracted**.
The **W2.6 verdict** is: "WAS OUR ERROR — do not build W3."

---

## Why the gap is so large

Open-Meteo's reanalysis smooths temperatures over a grid cell, which can differ
significantly from a point measurement at an airport station. Effects that contribute:

- **Urban heat island:** Airport stations in dense cities can be cooler than urban cores
  or warmer depending on local effects, but model grids average across both.
- **Coastal/elevation effects:** Some station locations are on reclaimed land or at slight
  elevation, producing micro-climates that model grids do not resolve.
- **Temporal sampling:** METAR records the maximum temperature over the full local calendar
  day; model reanalysis may use a different averaging window.

For 1°C-wide Polymarket buckets, errors of even 1°C change the resolution bucket entirely.
At 4.4°C (Seoul), three full buckets of error is catastrophic.

---

## The corrected methodology

Ground-truth temperature for resolution verification must come from **METAR/ASOS only**:

1. **Primary:** aviationweather.gov JSON API (AWC) — recent observations, ~120 h history
2. **Fallback:** IEM ASOS archive (mesonet.agron.iastate.edu) — reliable historical data

Both are free, no-authentication public APIs. The implementation is in
`weather_model/station_obs.py`.

Additional requirements:
- Fetch from the **resolution station's ICAO code** (e.g. RJTT for Tokyo Haneda),
  not a nearby proxy station
- Convert from UTC to the station's **local calendar day** before taking the daily maximum
  (implemented in `local_day_utc_window()`)
- Apply **WU round-half-up rounding** to get the bucket label
  (implemented in `round_half_up()`)

---

## Permanent rule

> **Never use Open-Meteo or any model reanalysis output as the realized temperature for
> bucket resolution verification. Only METAR/ASOS from the named station is valid.**

This rule is enforced in all subsequent weather research phases (W2.7 / H3).

---

## Lesson for prediction market research

Measurement bugs are particularly dangerous in prediction market research because they
are self-reinforcing: an underestimated realized temperature makes your model's prediction
look accurate, which in turn makes the market look wrong, which produces a spurious
positive edge signal.

The correct verification pipeline is:
1. Identify the resolution source in the market specification
2. Obtain the realized value from that exact source (or its most direct proxy)
3. Confirm that the independent verification data and the resolution source agree

Substituting a convenient model product for the authoritative station reading introduces
systematic bias of unknown magnitude and direction. In this case it was large enough to
flip the entire verdict.
