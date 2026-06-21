"""
Market discovery for Polymarket temperature markets.

Discovery method: slug reconstruction (NOT broad-listing endpoint).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
W0 probing confirmed that Polymarket's tag_slug=weather endpoint returns
climate / sea-ice events — it does NOT return daily-high temperature markets.
The only working discovery method is slug reconstruction:

    highest-temperature-in-{city}-on-{month}-{day}-{year}
    lowest-temperature-in-{city}-on-{month}-{day}-{year}

where month is the lowercase full English name, day is an integer without
leading zeros, and city is the slug fragment used by Polymarket (e.g. "nyc"
not "new-york").  Each slug maps to one GET request on the Gamma events API.
Source: W0 feasibility probe + confirmed working on London and NYC slugs.

Description parsing vs cities.py
──────────────────────────────────
Description parsing is done WITHOUT depending on cities.py.  The description
free text is the ground truth for station, unit, and rounding regime.
cities.py is used afterwards as a coordinate validation / override layer.

Coordinate resolution priority:
 1. Match station ICAO code against cities.py registry (City.icao).
 2. Fall back to _ICAO_SUPPLEMENT (hardcoded coords for extra cities).
 3. Fall back to city-slug → registry-city name mapping.
 4. All fail → coords=None, coords_unknown=True (flag for manual registry add).

This design allows the scanner to find markets for cities not yet in cities.py
(Madrid, Beijing, etc.) and surface their correct station/unit/regime from the
API description while flagging them for registry addition.

°F range-bucket note
─────────────────────
US °F markets use 2°F-wide range buckets ("78-79°F").  W1's bucket_model uses
1-degree single buckets and cannot process 2-wide ranges without modification.
All °F buckets are therefore flagged model_compatible=False.  The smell-test
runs the full model pipeline only on °C single-degree markets.  Extending
bucket_model for 2-wide ranges is a W3+ follow-up.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from weather_model.cities import ROUNDING_HKO, ROUNDING_WU, all_cities

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GAMMA_BASE = "https://gamma-api.polymarket.com/events"
_DEFAULT_TIMEOUT = 12  # seconds

_MONTH_NAMES = [
    "", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

# Candidate city slugs scanned during discovery.
# Beyond the 8 cities in cities.py; slugs use Polymarket's own naming
# (e.g. "nyc" not "new-york", "hong-kong" with a hyphen).
_CANDIDATE_CITY_SLUGS: List[str] = [
    # W1 registry cities
    "london", "paris", "nyc", "houston", "shanghai", "tokyo", "seoul", "hong-kong",
    # Extended candidates
    "madrid", "beijing", "los-angeles", "berlin", "moscow", "buenos-aires",
    "dubai", "sydney", "chicago", "miami", "toronto", "singapore", "amsterdam",
]

# ICAO code → (lat, lon) supplement for cities not yet in cities.py.
# Used when a discovered station ICAO doesn't match any cities.py entry.
# Coordinates are resolution-station coords (airport), not city center.
_ICAO_SUPPLEMENT: Dict[str, Tuple[float, float]] = {
    "LEMD": (40.4936, -3.5668),    # Madrid Barajas
    "ZBAA": (40.0799, 116.5844),   # Beijing Capital
    "KLAX": (33.9425, -118.4081),  # Los Angeles
    "EDDB": (52.3667, 13.5033),    # Berlin Brandenburg
    "UUEE": (55.9726, 37.4146),    # Moscow Sheremetyevo
    "SAEZ": (-34.8222, -58.5358),  # Buenos Aires Ezeiza
    "OMDB": (25.2528, 55.3644),    # Dubai
    "YSSY": (-33.9461, 151.1772),  # Sydney
    "KORD": (41.9742, -87.9073),   # Chicago O'Hare
    "KMIA": (25.7959, -80.2870),   # Miami
    "CYYZ": (43.6772, -79.6306),   # Toronto Pearson
    "WSSS": (1.3644, 103.9915),    # Singapore Changi
    "EHAM": (52.3086, 4.7639),     # Amsterdam Schiphol
    "KLGA": (40.7772, -73.8726),   # LaGuardia (matches cities.py "New York")
    "KHOU": (29.6454, -95.2789),   # Hobby (matches cities.py "Houston")
}

# Slug → canonical city name in cities.py (for coordinate fallback).
_SLUG_TO_REGISTRY_NAME: Dict[str, str] = {
    "london": "london",
    "paris": "paris",
    "nyc": "new york",
    "houston": "houston",
    "shanghai": "shanghai",
    "tokyo": "tokyo",
    "seoul": "incheon",
    "hong-kong": "hong kong",
}

# ---------------------------------------------------------------------------
# Regex patterns for bucket title parsing
# ---------------------------------------------------------------------------

_RE_RANGE_LOWER = re.compile(
    r"^(-?\d+)\s*-\s*(-?\d+)\s*°\s*([CF])\s+or\s+below\s*$", re.IGNORECASE
)
_RE_RANGE_UPPER = re.compile(
    r"^(-?\d+)\s*-\s*(-?\d+)\s*°\s*([CF])\s+or\s+higher\s*$", re.IGNORECASE
)
_RE_RANGE = re.compile(
    r"^(-?\d+)\s*-\s*(-?\d+)\s*°\s*([CF])\s*$", re.IGNORECASE
)
_RE_LOWER = re.compile(
    r"^(-?\d+)\s*°\s*([CF])\s+or\s+below\s*$", re.IGNORECASE
)
_RE_UPPER = re.compile(
    r"^(-?\d+)\s*°\s*([CF])\s+or\s+higher\s*$", re.IGNORECASE
)
_RE_SINGLE = re.compile(
    r"^(-?\d+)\s*°\s*([CF])\s*$", re.IGNORECASE
)

# Regex for Wunderground URL in description.
_WU_URL_RE = re.compile(
    r"https?://(?:www\.)?wunderground\.com/[^\s\"'<>]+", re.IGNORECASE
)
# Station name fallback: "Resolution source: X, {Station Name}."
_STATION_NAME_RE = re.compile(
    r"Resolution source:\s*[^,]+,\s*([^.(]+)(?:\s*\(|\s*\.)", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class MarketBucket:
    """One price bucket in a temperature market."""

    value: int               # Temperature label (integer degrees).  For range
                             # buckets this is lo.
    is_lower_tail: bool = False
    is_upper_tail: bool = False
    is_range: bool = False   # True for "78-79°F" style 2-wide buckets.
    lo: Optional[int] = None # Range lo (°F range buckets only).
    hi: Optional[int] = None # Range hi (°F range buckets only).
    unit: str = "C"          # "C" or "F"
    model_compatible: bool = True  # False for all °F buckets (W1 can't handle 2-wide)

    yes_token_id: str = ""
    no_token_id: str = ""
    yes_outcome_price: float = 0.0
    no_outcome_price: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    volume: float = 0.0


@dataclass
class WeatherMarket:
    """Normalized representation of a Polymarket temperature market."""

    city: str              # Slug city (e.g. "london", "madrid").
    date: str              # YYYY-MM-DD target resolution date.
    high_or_low: str       # "highest" or "lowest".
    resolution_station: str  # Station name or ICAO code parsed from description.
    station_url: Optional[str]  # Full WU URL if present in description.
    unit: str              # "C" or "F"
    rounding_regime: str   # ROUNDING_WU | ROUNDING_HKO
    end_date: str          # ISO8601 from Gamma endDate field.
    slug: str              # Raw Gamma slug.
    buckets: List[MarketBucket] = field(default_factory=list)
    coords: Optional[Tuple[float, float]] = None  # (lat, lon) for forecast fetch.
    coords_unknown: bool = False  # True when no coord source found; needs manual add.


# ---------------------------------------------------------------------------
# HTTP seam (monkeypatchable in tests)
# ---------------------------------------------------------------------------


def _http_get_json(url: str, params: Optional[Dict] = None) -> Optional[Any]:
    """Thin GET wrapper.  Returns parsed JSON or None on any error.

    Logs at DEBUG so the flood of expected 404s (slugs with no market)
    doesn't pollute WARNING logs during a scan.
    """
    try:
        resp = requests.get(url, params=params, timeout=_DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("HTTP GET no data: %s params=%s — %s", url, params, exc)
        return None


# ---------------------------------------------------------------------------
# Slug construction
# ---------------------------------------------------------------------------


def _make_slug(high_or_low: str, city_slug: str, target_date: date) -> str:
    """Build a Gamma event slug for one city + date.

    Example: _make_slug("highest", "london", date(2026, 6, 10))
             → "highest-temperature-in-london-on-june-10-2026"

    Day is written without leading zero (matches confirmed W0 live slugs).
    """
    month_name = _MONTH_NAMES[target_date.month]
    day = str(target_date.day)   # no leading zero
    year = str(target_date.year)
    return f"{high_or_low}-temperature-in-{city_slug}-on-{month_name}-{day}-{year}"


# ---------------------------------------------------------------------------
# Description parsing
# ---------------------------------------------------------------------------


def parse_market_description(
    description: str,
) -> Tuple[str, Optional[str], str, str]:
    """Parse a Gamma event description string.

    Returns
    -------
    (station_code, station_url, unit, rounding_regime)

    station_code : ICAO code extracted from a WU URL's last path segment, or
                   the station name text when no URL is present.
    station_url  : full Wunderground URL string if found, else None.
    unit         : "C" or "F"
    rounding_regime : ROUNDING_WU | ROUNDING_HKO
    """
    # --- station_url ---
    url_match = _WU_URL_RE.search(description)
    if url_match:
        raw_url = url_match.group(0)
        # Strip trailing sentence punctuation that the greedy pattern may capture
        # (e.g. "...LEMD." where "." ends the sentence).
        station_url: Optional[str] = re.sub(r"[.,;:!?]+$", "", raw_url)
    else:
        station_url = None

    # --- station code ---
    station_code: Optional[str] = None
    if station_url:
        # Extract last non-empty path segment (e.g. LEMD from ".../es/madrid/LEMD")
        segment = station_url.rstrip("/").split("/")[-1]
        # Accept 2–6 uppercase alphanumeric (ICAO + common WU station codes)
        if re.match(r"^[A-Z0-9]{2,8}$", segment, re.IGNORECASE):
            station_code = segment.upper()

    if station_code is None:
        # Fallback: "Resolution source: X, {Station Name}."
        m = _STATION_NAME_RE.search(description)
        station_code = m.group(1).strip() if m else "UNKNOWN"

    # --- unit ---
    desc_lower = description.lower()
    unit = "F" if "fahrenheit" in desc_lower else "C"

    # --- rounding regime ---
    hko_keywords = ("hong kong observatory", " hko", "one decimal", "tenths of")
    if any(kw in desc_lower for kw in hko_keywords):
        regime = ROUNDING_HKO
    else:
        regime = ROUNDING_WU

    return station_code, station_url, unit, regime


# ---------------------------------------------------------------------------
# Bucket title parsing
# ---------------------------------------------------------------------------


def parse_bucket_title(title: str) -> Optional[Dict]:
    """Parse a groupItemTitle string into bucket components.

    Returns a dict with keys:
        value          : int   (temperature label, or lo for range)
        lo             : Optional[int]  (range lo; None for single)
        hi             : Optional[int]  (range hi; None for single)
        is_lower_tail  : bool
        is_upper_tail  : bool
        is_range       : bool
        unit           : str  ("C" or "F")
        model_compatible : bool  (True only for °C single-degree buckets)

    Returns None if the title doesn't match any known pattern.
    """
    t = title.strip()

    # Check range patterns first (longer strings) before single patterns.

    m = _RE_RANGE_LOWER.match(t)
    if m:
        lo, hi, uc = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        return dict(value=lo, lo=lo, hi=hi, is_lower_tail=True, is_upper_tail=False,
                    is_range=True, unit=uc, model_compatible=False)

    m = _RE_RANGE_UPPER.match(t)
    if m:
        lo, hi, uc = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        return dict(value=lo, lo=lo, hi=hi, is_lower_tail=False, is_upper_tail=True,
                    is_range=True, unit=uc, model_compatible=False)

    m = _RE_RANGE.match(t)
    if m:
        lo, hi, uc = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        return dict(value=lo, lo=lo, hi=hi, is_lower_tail=False, is_upper_tail=False,
                    is_range=True, unit=uc, model_compatible=False)

    m = _RE_LOWER.match(t)
    if m:
        val, uc = int(m.group(1)), m.group(2).upper()
        return dict(value=val, lo=None, hi=None, is_lower_tail=True, is_upper_tail=False,
                    is_range=False, unit=uc, model_compatible=(uc == "C"))

    m = _RE_UPPER.match(t)
    if m:
        val, uc = int(m.group(1)), m.group(2).upper()
        return dict(value=val, lo=None, hi=None, is_lower_tail=False, is_upper_tail=True,
                    is_range=False, unit=uc, model_compatible=(uc == "C"))

    m = _RE_SINGLE.match(t)
    if m:
        val, uc = int(m.group(1)), m.group(2).upper()
        return dict(value=val, lo=None, hi=None, is_lower_tail=False, is_upper_tail=False,
                    is_range=False, unit=uc, model_compatible=(uc == "C"))

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_coords(
    station_code: str, city_slug: str
) -> Tuple[Optional[Tuple[float, float]], bool]:
    """Resolve (lat, lon) for a discovered market station.

    Returns (coords, coords_unknown) where coords is (lat, lon) or None.
    """
    code_upper = station_code.upper()

    # Pass 1: match against cities.py registry by ICAO.
    for city in all_cities():
        if city.icao.upper() == code_upper:
            return (city.lat, city.lon), False

    # Pass 2: _ICAO_SUPPLEMENT.
    if code_upper in _ICAO_SUPPLEMENT:
        return _ICAO_SUPPLEMENT[code_upper], False

    # Pass 3: slug → registry city name.
    registry_name = _SLUG_TO_REGISTRY_NAME.get(city_slug)
    if registry_name:
        from weather_model.cities import get_city  # local import to avoid circular ref risk
        try:
            c = get_city(registry_name)
            return (c.lat, c.lon), False
        except KeyError:
            pass

    return None, True


def _parse_bucket_dict(m: Dict, default_unit: str) -> Optional[MarketBucket]:
    """Parse one element of the Gamma 'markets' list into a MarketBucket."""
    title = m.get("groupItemTitle", "")
    parsed = parse_bucket_title(title)
    if parsed is None:
        logger.debug("Could not parse bucket title: %r", title)
        return None

    token_ids = m.get("clobTokenIds") or ["", ""]
    outcome_prices = m.get("outcomePrices") or ["0", "0"]

    def _f(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    return MarketBucket(
        value=parsed["value"],
        is_lower_tail=parsed["is_lower_tail"],
        is_upper_tail=parsed["is_upper_tail"],
        is_range=parsed["is_range"],
        lo=parsed["lo"],
        hi=parsed["hi"],
        unit=parsed["unit"],
        model_compatible=parsed["model_compatible"],
        yes_token_id=str(token_ids[0]) if len(token_ids) > 0 else "",
        no_token_id=str(token_ids[1]) if len(token_ids) > 1 else "",
        yes_outcome_price=_f(outcome_prices[0] if len(outcome_prices) > 0 else "0"),
        no_outcome_price=_f(outcome_prices[1] if len(outcome_prices) > 1 else "0"),
        best_bid=_f(m.get("bestBid")),
        best_ask=_f(m.get("bestAsk")),
        volume=_f(m.get("volume")),
    )


# ---------------------------------------------------------------------------
# Public event-dict parser (exposed for testing)
# ---------------------------------------------------------------------------


def parse_event_dict(
    event: Dict,
    city_slug: str,
    date_str: str,
    high_or_low: str,
) -> Optional[WeatherMarket]:
    """Parse a raw Gamma event dict into a WeatherMarket.

    Parameters
    ----------
    event       : dict as returned by the Gamma /events?slug= endpoint.
    city_slug   : the city part of the slug (e.g. "london").
    date_str    : resolution date in YYYY-MM-DD format.
    high_or_low : "highest" or "lowest".

    Returns None if the event has no usable markets.
    """
    description = event.get("description") or ""
    end_date = event.get("endDate") or ""
    slug = event.get("slug") or f"{high_or_low}-temperature-in-{city_slug}-on-{date_str}"
    raw_markets = event.get("markets") or []

    if not raw_markets:
        return None

    station_code, station_url, desc_unit, regime = parse_market_description(description)

    buckets: List[MarketBucket] = []
    for raw_m in raw_markets:
        b = _parse_bucket_dict(raw_m, desc_unit)
        if b is not None:
            buckets.append(b)

    if not buckets:
        return None

    # Derive authoritative unit from bucket titles (more reliable than description
    # text, which may mention both units or use them in a non-authoritative context).
    bucket_units = [b.unit for b in buckets]
    unit = max(set(bucket_units), key=bucket_units.count)  # majority-vote unit

    coords, coords_unknown = _resolve_coords(station_code, city_slug)

    return WeatherMarket(
        city=city_slug,
        date=date_str,
        high_or_low=high_or_low,
        resolution_station=station_code,
        station_url=station_url,
        unit=unit,
        rounding_regime=regime,
        end_date=end_date,
        slug=slug,
        buckets=buckets,
        coords=coords,
        coords_unknown=coords_unknown,
    )


# ---------------------------------------------------------------------------
# Main discovery entry point
# ---------------------------------------------------------------------------


def discover_markets(
    city_slugs: Optional[List[str]] = None,
    horizon_days: int = 3,
    high_and_low: bool = True,
    _sleep_between: float = 0.1,
) -> List[WeatherMarket]:
    """Discover currently-open Polymarket temperature markets.

    Iterates city_slugs × near-term dates × high/low prefixes, querying
    the Gamma events API for each slug.  Failed / missing slugs are silently
    skipped (404 is expected for most slug combinations).

    Parameters
    ----------
    city_slugs      : list of city slug fragments (e.g. ["london", "madrid"]).
                      Defaults to _CANDIDATE_CITY_SLUGS (~23 cities).
    horizon_days    : how many days ahead to scan (0 = today only, 3 = D+0..D+3).
    high_and_low    : if True, scan both "highest" and "lowest" slugs per city/date.
    _sleep_between  : seconds to sleep between HTTP requests (rate-limit courtesy).

    Returns
    -------
    List of WeatherMarket objects for every slug that returned a valid event.
    """
    if city_slugs is None:
        city_slugs = _CANDIDATE_CITY_SLUGS

    today = date.today()
    prefixes = ["highest", "lowest"] if high_and_low else ["highest"]
    results: List[WeatherMarket] = []

    for city_slug in city_slugs:
        for day_offset in range(horizon_days + 1):
            target_date = today + timedelta(days=day_offset)
            date_str = target_date.strftime("%Y-%m-%d")
            for prefix in prefixes:
                slug = _make_slug(prefix, city_slug, target_date)
                data = _http_get_json(_GAMMA_BASE, {"slug": slug})
                if _sleep_between > 0:
                    time.sleep(_sleep_between)
                if data is None:
                    continue
                # Gamma may return a list or a single dict.
                if isinstance(data, list):
                    if not data:
                        continue
                    event = data[0]
                elif isinstance(data, dict):
                    event = data
                else:
                    continue

                if not isinstance(event, dict):
                    continue

                market = parse_event_dict(event, city_slug, date_str, prefix)
                if market is not None:
                    results.append(market)

    return results
