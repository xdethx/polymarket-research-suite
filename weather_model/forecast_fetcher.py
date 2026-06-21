"""
Open-Meteo ensemble forecast fetcher for weather trading.

Read-only.  No orders, no disk writes, no side effects beyond HTTP GET.

Fetches daily maximum temperature ensemble members from Open-Meteo's free
ensemble API for a given resolution-station coordinate + target date.
Supports ECMWF IFS 025 (51 members) and GFS seamless (31 members).

Unit handling
─────────────
Open-Meteo always returns °C regardless of the market unit.  When the
market resolves in °F (US cities), callers pass unit="F" and this module
converts every member value via c_to_f().  Conversion is applied BEFORE
the members list is cached and returned, so callers always receive values
in the requested unit.

Thread-safe TTL cache
──────────────────────
Ensembles update ~every 6 hours.  Caching prevents redundant fetches when
the smell-test or recorder queries the same (lat, lon, date, model) multiple
times in a session.  TTL is 1 hour (_CACHE_TTL_S).  The cache stores the
raw member list (same object); callers must not mutate it.

The fetcher does NOT compute (μ, σ) — that is the caller's responsibility.
Probability math lives in weather_model/bucket_model.py (pure, network-free).
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from datetime import date as date_type
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_ENSEMBLE_BASE = "https://ensemble-api.open-meteo.com/v1/ensemble"
_FORECAST_BASE = "https://api.open-meteo.com/v1/forecast"
_DEFAULT_TIMEOUT = 15  # seconds; ensemble responses are large
_CACHE_TTL_S = 3600    # 1 hour

# ---------------------------------------------------------------------------
# Thread-safe TTL cache
# ---------------------------------------------------------------------------

_CACHE: Dict[Tuple, Tuple[List[float], float]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: Tuple) -> Optional[List[float]]:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        members, expiry = entry
        if time.monotonic() > expiry:
            del _CACHE[key]
            return None
        return members


def _cache_set(key: Tuple, members: List[float]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (members, time.monotonic() + _CACHE_TTL_S)


def clear_cache() -> None:
    """Evict all cached entries.  Useful in tests."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# HTTP seam (monkeypatchable in tests)
# ---------------------------------------------------------------------------


def _http_get_json(url: str, params: Optional[Dict] = None) -> Optional[Any]:
    """Thin GET wrapper.  Returns parsed JSON or None on any error."""
    try:
        resp = requests.get(url, params=params, timeout=_DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("forecast_fetcher HTTP GET failed: %s — %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Unit conversion
# ---------------------------------------------------------------------------


def c_to_f(temp_c: float) -> float:
    """Convert Celsius to Fahrenheit.

    Used when the market unit is °F and Open-Meteo returned °C.
    Exposed as a public helper so tests can verify it independently.
    """
    return temp_c * 9.0 / 5.0 + 32.0


# ---------------------------------------------------------------------------
# Internal: parse ensemble members from an Open-Meteo daily response
# ---------------------------------------------------------------------------


def _parse_members(data: Dict, target_date_str: str) -> Optional[List[float]]:
    """Extract the temperature_2m_max members for target_date_str.

    Open-Meteo daily response structure:
        {
          "daily": {
            "time": ["2026-06-09", "2026-06-10", ...],
            "temperature_2m_max": [ctrl_val0, ctrl_val1, ...],
            "temperature_2m_max_member01": [...],
            ...
          }
        }

    Returns a list of float temperatures (control + all member values at the
    target date index), or None when the date isn't in the response or no
    members are present.
    """
    daily = data.get("daily") or {}
    times: List[str] = daily.get("time") or []

    if target_date_str not in times:
        logger.debug("Target date %s not in Open-Meteo response times %s",
                     target_date_str, times)
        return None

    idx = times.index(target_date_str)
    members: List[float] = []

    # Control member: key "temperature_2m_max"
    ctrl_vals = daily.get("temperature_2m_max")
    if ctrl_vals and idx < len(ctrl_vals) and ctrl_vals[idx] is not None:
        members.append(float(ctrl_vals[idx]))

    # Perturbed members: "temperature_2m_max_member01", "member02", ...
    i = 1
    while True:
        key = f"temperature_2m_max_member{i:02d}"
        vals = daily.get(key)
        if vals is None:
            break  # no more member keys
        if idx < len(vals) and vals[idx] is not None:
            members.append(float(vals[idx]))
        i += 1

    return members if members else None


# ---------------------------------------------------------------------------
# Public: fetch ensemble members
# ---------------------------------------------------------------------------


def fetch_ensemble(
    lat: float,
    lon: float,
    date_str: str,
    model: str,
    unit: str = "C",
) -> Optional[List[float]]:
    """Fetch daily max-temperature ensemble members for one station + date.

    Parameters
    ----------
    lat, lon  : Resolution-station coordinates (NOT city center).
    date_str  : Target date in YYYY-MM-DD format.
    model     : Open-Meteo model name.  Supported:
                  "ecmwf_ifs025" — 51 members (control + 50 perturbed)
                  "gfs_seamless" — 31 members (control + 30 perturbed)
    unit      : "C" (default) or "F".  Open-Meteo always fetches °C;
                members are converted to °F before caching when unit="F".

    Returns
    -------
    List of float temperatures in the requested unit, one per ensemble
    member.  Returns None on network error or when the date is not in the
    forecast window.

    Caching
    -------
    Results are cached in-process for _CACHE_TTL_S seconds keyed by
    (lat, lon, date_str, model, unit).  The returned list is the cached
    object itself — do NOT mutate it.
    """
    cache_key = (lat, lon, date_str, model, unit)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Compute how many forecast days are needed.
    try:
        today = date_type.today()
        target = date_type.fromisoformat(date_str)
        delta = (target - today).days
    except ValueError:
        logger.warning("Invalid date_str: %r", date_str)
        return None

    if delta < 0:
        logger.debug("Cannot fetch past date %s with ensemble endpoint", date_str)
        return None

    forecast_days = max(delta + 2, 2)  # +2 buffer so the date lands in the window

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",  # always fetch °C; convert below if needed
        "forecast_days": forecast_days,
        "models": model,
        "timezone": "UTC",
    }

    data = _http_get_json(_ENSEMBLE_BASE, params)
    if data is None:
        return None

    raw_members = _parse_members(data, date_str)
    if raw_members is None:
        return None

    # Convert to °F if requested.
    if unit == "F":
        raw_members = [c_to_f(t) for t in raw_members]

    _cache_set(cache_key, raw_members)
    return raw_members


# ---------------------------------------------------------------------------
# Public: compute (μ, σ) from members
# ---------------------------------------------------------------------------


def members_to_mu_sigma(members: List[float]) -> Tuple[float, float]:
    """Compute (mean, population std dev) from a list of ensemble members.

    Uses population std dev (pstdev, divides by N) since the ensemble IS
    the full forecast distribution — we're not estimating a population from
    a sample.

    Minimum sigma is clamped to 0.01 to avoid degenerate zero-sigma inputs
    to blend_models() on extremely tight ensembles.

    Returns
    -------
    (mu, sigma) where both are floats.  sigma >= 0.01.
    """
    if not members:
        raise ValueError("members list must not be empty")
    mu = statistics.mean(members)
    sigma = statistics.pstdev(members) if len(members) > 1 else 0.0
    sigma = max(sigma, 0.01)
    return mu, sigma


# ---------------------------------------------------------------------------
# Public: current-day max-so-far (intraday angle, best-effort)
# ---------------------------------------------------------------------------


def fetch_current_max_so_far(
    lat: float,
    lon: float,
    current_utc_hour: Optional[int] = None,
) -> Optional[Tuple[float, int]]:
    """Fetch the observed/short-range max temperature so far today (UTC).

    Uses Open-Meteo's regular forecast endpoint (api.open-meteo.com) with
    hourly temperature_2m for today.  The first N UTC hours are ERA5
    near-real-time observations; later hours are short-range NWP.
    Both are returned seamlessly — W2 does NOT distinguish obs from forecast.

    Parameters
    ----------
    lat, lon         : Station coordinates (°).
    current_utc_hour : Override the current UTC hour (0-23).  Defaults to
                       datetime.utcnow().hour.

    Returns
    -------
    (max_so_far_celsius, up_to_hour) where up_to_hour is the last hour
    included (0-indexed UTC), or None on failure.
    """
    import datetime  # local import to keep module top clean

    if current_utc_hour is None:
        current_utc_hour = datetime.datetime.utcnow().hour

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "forecast_days": 1,
        "timezone": "UTC",
    }
    data = _http_get_json(_FORECAST_BASE, params)
    if data is None:
        return None

    hourly = data.get("hourly") or {}
    temps: List = hourly.get("temperature_2m") or []

    if not temps:
        return None

    # Include hours 0 through current_utc_hour (inclusive).
    up_to = min(current_utc_hour + 1, len(temps))
    past_temps = [t for t in temps[:up_to] if t is not None]
    if not past_temps:
        return None

    return max(past_temps), current_utc_hour
