"""
METAR/ASOS realized-high client for Polymarket weather station resolution.

Fetches the daily maximum 2m-air temperature for a named ICAO station over
the station's LOCAL calendar day (not a UTC window).  Two independent sources:

  1. aviationweather.gov JSON API (AWC)  — recent window, ~120 h history
  2. IEM ASOS archive (mesonet.agron.iastate.edu) — reliable historical

The caller determines which UTC window corresponds to the local calendar day
via ``local_day_utc_window()`` and passes it to ``fetch_daily_max()``.

``round_half_up()`` applies WU's whole-degree rule: floor(temp + 0.5).

No API key required for either source.  Read-only HTTP only.

Why METAR/ASOS, not Open-Meteo?
---------------------------------
The W2.6 edge-hardening phase found that Open-Meteo reanalysis
(forecast+past_days=2) underestimated actual station temperatures by
0.9–4.4°C across East Asian cities, causing bucket-level measurement
errors on 1°C-wide markets.  METAR/ASOS is the only reliable ground
truth for resolution verification.  See findings/weather-metar-lesson.md.
"""

from __future__ import annotations

import datetime
import math
from typing import List, Optional, Tuple

import requests

_TIMEOUT = 15  # seconds
_AWC_BASE = "https://aviationweather.gov/api/data/metar"
_IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------


def round_half_up(temp_c: float) -> int:
    """WU whole-degree rounding: floor(temp + 0.5).

    This reproduces the WU round-half-up rule:
        16.5  →  17   (rounds up)
        16.49 →  16   (rounds down)
        16.0  →  16
    """
    return int(math.floor(temp_c + 0.5))


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def local_day_utc_window(
    date_str: str, utc_offset_hours: int
) -> Tuple[datetime.datetime, datetime.datetime]:
    """Return (utc_start, utc_end) for the station-local calendar day.

    Args:
        date_str: ISO date string, e.g. "2026-06-09".
        utc_offset_hours: Station UTC offset in whole hours (positive = east).
            Asia/Tokyo (JST) = +9, Asia/Seoul (KST) = +9,
            Asia/Shanghai (CST) = +8, Europe/London (BST, June) = +1.

    Returns:
        (utc_start, utc_end) as UTC-aware datetime objects.

    Example:
        Tokyo (UTC+9) local June 9 calendar day:
          utc_start = 2026-06-08 15:00 UTC
          utc_end   = 2026-06-09 15:00 UTC
    """
    local_date = datetime.date.fromisoformat(date_str)
    # Local midnight on date_str = UTC midnight minus the offset
    utc_start = datetime.datetime(
        local_date.year,
        local_date.month,
        local_date.day,
        0, 0, 0,
        tzinfo=datetime.timezone.utc,
    ) - datetime.timedelta(hours=utc_offset_hours)
    utc_end = utc_start + datetime.timedelta(hours=24)
    return utc_start, utc_end


# ---------------------------------------------------------------------------
# AWC fetch
# ---------------------------------------------------------------------------


def _parse_awc_timestamp(raw: object) -> Optional[datetime.datetime]:
    """Parse AWC obsTime field — either int Unix ts or ISO string."""
    if isinstance(raw, (int, float)):
        return datetime.datetime.fromtimestamp(float(raw), tz=datetime.timezone.utc)
    if isinstance(raw, str):
        s = raw.strip().rstrip("Z")
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                return datetime.datetime.strptime(s, fmt).replace(
                    tzinfo=datetime.timezone.utc
                )
            except ValueError:
                continue
    return None


def _fetch_awc(icao: str, hours_back: int = 96) -> Optional[List[Tuple[datetime.datetime, float]]]:
    """Fetch recent METAR observations from aviationweather.gov.

    Returns list of (valid_utc, temp_c) sorted by time, or None on failure.
    Temperature field is ``temp`` (Celsius) in the AWC JSON schema.
    """
    try:
        resp = requests.get(
            _AWC_BASE,
            params={"ids": icao, "format": "json", "hours": hours_back},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list):
            return None
        out: List[Tuple[datetime.datetime, float]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            # AWC uses "obsTime" as the field name
            raw_ts = row.get("obsTime") or row.get("validTime") or row.get("reportTime")
            raw_temp = row.get("temp")
            if raw_ts is None or raw_temp is None:
                continue
            ts = _parse_awc_timestamp(raw_ts)
            if ts is None:
                continue
            try:
                out.append((ts, float(raw_temp)))
            except (ValueError, TypeError):
                continue
        return out if out else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# IEM ASOS archive fetch
# ---------------------------------------------------------------------------


def _fetch_iem(
    icao: str,
    utc_start: datetime.datetime,
    utc_end: datetime.datetime,
) -> Optional[List[Tuple[datetime.datetime, float]]]:
    """Fetch temperature observations from IEM ASOS archive.

    Queries a window one day wider than requested (±1 day) so sub-hour
    boundaries are always covered; caller must filter to the exact window.

    Returns list of (valid_utc, temp_c) or None on failure.
    """
    # Expand window by ±1 day to ensure no boundary miss
    d1 = (utc_start - datetime.timedelta(days=1)).date()
    d2 = (utc_end + datetime.timedelta(days=1)).date()
    try:
        resp = requests.get(
            _IEM_BASE,
            params={
                "station": icao,
                "data": "tmpc",
                "year1": d1.year,
                "month1": d1.month,
                "day1": d1.day,
                "year2": d2.year,
                "month2": d2.month,
                "day2": d2.day,
                "tz": "UTC",
                "format": "onlycomma",
                "latlon": "no",
                "elev": "no",
                "missing": "M",
                "trace": "T",
                "direct": "no",
                "report_type": "3",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        lines = resp.text.strip().splitlines()
        out: List[Tuple[datetime.datetime, float]] = []
        for line in lines:
            line = line.strip()
            # Skip comment lines and the header
            if line.startswith("#") or line.startswith("station"):
                continue
            parts = line.split(",")
            if len(parts) < 3:
                continue
            ts_str = parts[1].strip()
            temp_str = parts[2].strip()
            if temp_str in ("M", "T", ""):
                continue  # missing or trace
            try:
                ts = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M").replace(
                    tzinfo=datetime.timezone.utc
                )
                out.append((ts, float(temp_str)))
            except (ValueError, TypeError):
                continue
        return out if out else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Window filter
# ---------------------------------------------------------------------------


def _max_in_window(
    obs: List[Tuple[datetime.datetime, float]],
    utc_start: datetime.datetime,
    utc_end: datetime.datetime,
) -> Optional[float]:
    """Return max temperature from obs within [utc_start, utc_end).

    Returns None if no observations fall in the window.
    """
    temps = [t for ts, t in obs if utc_start <= ts < utc_end]
    return max(temps) if temps else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_daily_max(
    icao: str,
    date_str: str,
    utc_offset_hours: int,
) -> Optional[Tuple[float, str]]:
    """Return (daily_max_celsius, source) for the station-local calendar day.

    Tries AWC first; falls back to IEM.  Returns None if both fail or return
    no data within the local-day window.

    Args:
        icao:              Station ICAO code (e.g. "RJTT", "ZSPD", "RKSI").
        date_str:          ISO date string for the LOCAL calendar day,
                           e.g. "2026-06-09".
        utc_offset_hours:  UTC offset in whole hours (Asia/Tokyo = +9).

    Returns:
        (max_temp_c, source_name) where source_name ∈ {"AWC", "IEM"}.
        None if both sources fail.
    """
    utc_start, utc_end = local_day_utc_window(date_str, utc_offset_hours)

    # Compute how many hours back the window start is from now
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    hours_back = max(72, int((now_utc - utc_start).total_seconds() / 3600) + 12)
    hours_back = min(hours_back, 120)  # AWC cap

    # 1. Try AWC
    awc_obs = _fetch_awc(icao, hours_back=hours_back)
    if awc_obs:
        val = _max_in_window(awc_obs, utc_start, utc_end)
        if val is not None:
            return val, "AWC"

    # 2. Fall back to IEM ASOS archive
    iem_obs = _fetch_iem(icao, utc_start, utc_end)
    if iem_obs:
        val = _max_in_window(iem_obs, utc_start, utc_end)
        if val is not None:
            return val, "IEM"

    return None
