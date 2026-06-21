"""
City registry for weather trading.

Maps city names to resolution-station metadata:
  ICAO code, lat/lon, resolution source, temperature unit,
  and rounding regime used by Polymarket settlement.

Coordinate rationale: forecasts must target the RESOLUTION STATION's
coordinates, not city-center coordinates.  The difference can be 3-8°F
(confirmed in W0 research — see findings/crypto-discovery-engine.md).
Using wrong coordinates introduces systematic error that dominates any edge.

Pure data + small lookup helpers.  No network, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

# ---------------------------------------------------------------------------
# Rounding regime constants
# These match the constants in bucket_model.py — defined here so callers
# can reference them without importing the model layer.
# ---------------------------------------------------------------------------

# WU whole-degree round-half-up: bucket N covers [N-0.5, N+0.5).
# Used by all Wunderground-resolution markets (London, Paris, NY, etc.)
ROUNDING_WU = "wu_round_half_up"

# HKO one-decimal floor: HKO reports to 0.1°C, Polymarket floors to integer.
# Bucket N covers [N, N+1).  Currently only confirmed for Hong Kong.
ROUNDING_HKO = "hko_floor"


@dataclass(frozen=True)
class City:
    """Resolution-station metadata for a Polymarket weather market city."""
    name: str
    icao: str                  # ICAO station code for the resolution station
    lat: float                 # Resolution station latitude
    lon: float                 # Resolution station longitude
    resolution_source: str     # "wunderground" | "hko" | "metoffice" | "nws"
    unit: str                  # "C" | "F"
    rounding_regime: str       # ROUNDING_WU | ROUNDING_HKO


# ---------------------------------------------------------------------------
# Registry — built from W0 feasibility research, station table.
#
# Important notes:
#   Paris → LFPB (Le Bourget), NOT LFPG (CDG).
#     Polymarket description: "Paris Le Bourget".  CDG would introduce ~1-3°C error.
#   Hong Kong → VHHH (Chek Lap Kok, ICAO).  Polymarket resolves on HKO (Hong Kong
#     Observatory), but the forecast coordinate matches the airport station.
#     Rounding regime is hko_floor — the only confirmed non-WU regime.
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, City] = {c.name.lower(): c for c in [
    City("London",    "EGLC",  51.5048,   0.0522,  "wunderground", "C", ROUNDING_WU),
    City("Paris",     "LFPB",  48.9962,   2.5979,  "wunderground", "C", ROUNDING_WU),
    City("New York",  "KLGA",  40.7772, -73.8726,  "wunderground", "F", ROUNDING_WU),
    City("Houston",   "KHOU",  29.6454, -95.2789,  "wunderground", "F", ROUNDING_WU),
    City("Shanghai",  "ZSPD",  31.1443, 121.8083,  "wunderground", "C", ROUNDING_WU),
    City("Tokyo",     "RJTT",  35.7647, 140.3864,  "wunderground", "C", ROUNDING_WU),
    City("Incheon",   "RKSI",  37.4691, 126.4505,  "wunderground", "C", ROUNDING_WU),
    City("Hong Kong", "VHHH",  22.308,  114.174,   "hko",          "C", ROUNDING_HKO),
]}


def get_city(name: str) -> City:
    """Return City by name (case-insensitive).

    Raises KeyError if the city is not registered.
    """
    key = name.strip().lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown city: {name!r}. Known cities: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[key]


def all_cities() -> List[City]:
    """Return all registered cities in insertion order."""
    return list(_REGISTRY.values())
