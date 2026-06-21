"""Tests for weather_model/gamma_discovery.py — offline parsing only; no live network."""
import json
import os

import pytest

from weather_model.cities import ROUNDING_HKO, ROUNDING_WU
from weather_model.gamma_discovery import (
    WeatherMarket,
    parse_bucket_title,
    parse_event_dict,
    parse_market_description,
)

# ---------------------------------------------------------------------------
# Fixture loader
# ---------------------------------------------------------------------------

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name: str) -> dict:
    with open(os.path.join(_FIXTURE_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# parse_market_description — station + unit + rounding regime
# ---------------------------------------------------------------------------


class TestParseDescriptionCelsiusWithUrl:
    """Madrid-style: Wunderground URL present → station code from URL."""

    def setup_method(self):
        event = _load("gamma_event_celsius_url.json")
        self.station, self.url, self.unit, self.regime = parse_market_description(
            event["description"]
        )

    def test_station_code_extracted_from_url(self):
        assert self.station == "LEMD"

    def test_station_url_captured(self):
        assert self.url is not None
        assert "wunderground.com" in self.url
        assert "LEMD" in self.url

    def test_unit_celsius(self):
        assert self.unit == "C"

    def test_regime_wu(self):
        assert self.regime == ROUNDING_WU


class TestParseDescriptionLondonStyle:
    """London-style: no URL in description → station name fallback."""

    def setup_method(self):
        desc = (
            "Resolution source: Wunderground, London City Airport Station. "
            "Measures temperatures in whole degrees Celsius."
        )
        self.station, self.url, self.unit, self.regime = parse_market_description(desc)

    def test_no_url(self):
        assert self.url is None

    def test_station_name_extracted(self):
        # Should capture "London City Airport Station" or similar
        assert "London" in self.station or "Airport" in self.station or self.station != "UNKNOWN"

    def test_unit_celsius(self):
        assert self.unit == "C"

    def test_regime_wu(self):
        assert self.regime == ROUNDING_WU


class TestParseDescriptionFahrenheit:
    """NYC-style: Fahrenheit, station name fallback (KLGA in parentheses)."""

    def setup_method(self):
        event = _load("gamma_event_fahrenheit.json")
        self.station, self.url, self.unit, self.regime = parse_market_description(
            event["description"]
        )

    def test_unit_fahrenheit(self):
        assert self.unit == "F"

    def test_regime_wu(self):
        assert self.regime == ROUNDING_WU


class TestParseDescriptionHKO:
    """Hong Kong Observatory → ROUNDING_HKO."""

    def setup_method(self):
        event = _load("gamma_event_hko.json")
        self.station, self.url, self.unit, self.regime = parse_market_description(
            event["description"]
        )

    def test_regime_hko(self):
        assert self.regime == ROUNDING_HKO

    def test_unit_celsius(self):
        assert self.unit == "C"

    def test_no_url(self):
        assert self.url is None


# ---------------------------------------------------------------------------
# parse_bucket_title — all pattern variants
# ---------------------------------------------------------------------------


class TestParseBucketTitleCelsius:
    def test_interior_single(self):
        r = parse_bucket_title("30°C")
        assert r is not None
        assert r["value"] == 30
        assert not r["is_lower_tail"]
        assert not r["is_upper_tail"]
        assert not r["is_range"]
        assert r["unit"] == "C"
        assert r["model_compatible"] is True

    def test_lower_tail(self):
        r = parse_bucket_title("27°C or below")
        assert r is not None
        assert r["value"] == 27
        assert r["is_lower_tail"] is True
        assert r["is_upper_tail"] is False
        assert r["model_compatible"] is True

    def test_upper_tail(self):
        r = parse_bucket_title("33°C or higher")
        assert r is not None
        assert r["value"] == 33
        assert r["is_upper_tail"] is True
        assert r["is_lower_tail"] is False
        assert r["model_compatible"] is True

    def test_negative_value(self):
        r = parse_bucket_title("-5°C")
        assert r is not None
        assert r["value"] == -5
        assert r["model_compatible"] is True

    def test_case_insensitive_unit(self):
        r = parse_bucket_title("16°c")
        assert r is not None
        assert r["unit"] == "C"

    def test_unrecognized_returns_none(self):
        assert parse_bucket_title("unknown bucket") is None

    def test_empty_returns_none(self):
        assert parse_bucket_title("") is None


class TestParseBucketTitleFahrenheit:
    def test_range_interior(self):
        r = parse_bucket_title("78-79°F")
        assert r is not None
        assert r["is_range"] is True
        assert r["lo"] == 78
        assert r["hi"] == 79
        assert r["value"] == 78
        assert r["unit"] == "F"
        assert r["model_compatible"] is False

    def test_range_lower_tail(self):
        r = parse_bucket_title("72-73°F or below")
        assert r is not None
        assert r["is_range"] is True
        assert r["is_lower_tail"] is True
        assert r["lo"] == 72
        assert r["hi"] == 73
        assert r["model_compatible"] is False

    def test_range_upper_tail(self):
        r = parse_bucket_title("86-87°F or higher")
        assert r is not None
        assert r["is_range"] is True
        assert r["is_upper_tail"] is True
        assert r["model_compatible"] is False

    def test_single_lower_tail_fahrenheit(self):
        r = parse_bucket_title("74°F or below")
        assert r is not None
        assert r["is_lower_tail"] is True
        assert r["unit"] == "F"
        assert r["model_compatible"] is False

    def test_single_upper_tail_fahrenheit(self):
        r = parse_bucket_title("86°F or higher")
        assert r is not None
        assert r["is_upper_tail"] is True
        assert r["unit"] == "F"
        assert r["model_compatible"] is False


# ---------------------------------------------------------------------------
# parse_event_dict — end-to-end from fixture
# ---------------------------------------------------------------------------


class TestParseEventDictCelsiusUrl:
    def setup_method(self):
        self.event = _load("gamma_event_celsius_url.json")
        self.market = parse_event_dict(self.event, "madrid", "2026-06-10", "highest")

    def test_returns_weather_market(self):
        assert isinstance(self.market, WeatherMarket)

    def test_station_code(self):
        assert self.market.resolution_station == "LEMD"

    def test_unit(self):
        assert self.market.unit == "C"

    def test_regime(self):
        assert self.market.rounding_regime == ROUNDING_WU

    def test_bucket_count(self):
        assert len(self.market.buckets) == 7

    def test_first_bucket_is_lower_tail(self):
        assert self.market.buckets[0].is_lower_tail is True
        assert self.market.buckets[0].value == 27

    def test_last_bucket_is_upper_tail(self):
        assert self.market.buckets[-1].is_upper_tail is True
        assert self.market.buckets[-1].value == 33

    def test_interior_buckets_model_compatible(self):
        interior = [b for b in self.market.buckets if not b.is_lower_tail and not b.is_upper_tail]
        assert all(b.model_compatible for b in interior)

    def test_yes_token_ids_populated(self):
        for b in self.market.buckets:
            assert b.yes_token_id != ""

    def test_outcome_prices_parsed(self):
        modal = self.market.buckets[3]  # 30°C at index 3
        assert abs(modal.yes_outcome_price - 0.35) < 0.001

    def test_coords_resolved_from_supplement(self):
        # LEMD is in _ICAO_SUPPLEMENT
        assert self.market.coords is not None
        assert self.market.coords_unknown is False
        lat, lon = self.market.coords
        assert 40.0 < lat < 41.0
        assert -4.0 < lon < -3.0

    def test_high_or_low(self):
        assert self.market.high_or_low == "highest"

    def test_date(self):
        assert self.market.date == "2026-06-10"


class TestParseEventDictFahrenheit:
    def setup_method(self):
        event = _load("gamma_event_fahrenheit.json")
        self.market = parse_event_dict(event, "nyc", "2026-06-10", "highest")

    def test_unit_fahrenheit(self):
        assert self.market.unit == "F"

    def test_all_buckets_model_incompatible(self):
        assert all(not b.model_compatible for b in self.market.buckets)

    def test_range_bucket_parsed(self):
        # Find "78-79°F"
        range_b = next((b for b in self.market.buckets if b.lo == 78), None)
        assert range_b is not None
        assert range_b.hi == 79
        assert range_b.is_range is True

    def test_lower_tail_single(self):
        lower = self.market.buckets[0]
        assert lower.is_lower_tail is True
        assert lower.value == 74


class TestParseEventDictHKO:
    def setup_method(self):
        event = _load("gamma_event_hko.json")
        self.market = parse_event_dict(event, "hong-kong", "2026-06-10", "highest")

    def test_regime_hko(self):
        assert self.market.rounding_regime == ROUNDING_HKO

    def test_coords_from_registry(self):
        # "hong-kong" maps to cities.py registry entry (VHHH)
        assert self.market.coords is not None
        assert self.market.coords_unknown is False


class TestParseEventDictCoordsUnknown:
    """A station code not in registry or supplement → coords_unknown=True."""

    def test_unknown_station(self):
        event = {
            "title": "Highest temperature in XYZ?",
            "slug": "highest-temperature-in-xyz-on-june-10-2026",
            "endDate": "2026-06-10T12:00:00Z",
            "description": "Resolution source: SomeObs, XYZ International Airport Station. Measures temperatures in whole degrees Celsius.",
            "markets": [
                {"groupItemTitle": "20°C or below", "clobTokenIds": ["x1", "x2"],
                 "outcomePrices": ["0.1", "0.9"], "bestBid": "0.09", "bestAsk": "0.11", "volume": "50"},
                {"groupItemTitle": "25°C or higher", "clobTokenIds": ["y1", "y2"],
                 "outcomePrices": ["0.9", "0.1"], "bestBid": "0.89", "bestAsk": "0.91", "volume": "50"},
            ],
        }
        market = parse_event_dict(event, "xyz", "2026-06-10", "highest")
        assert market is not None
        assert market.coords_unknown is True
        assert market.coords is None


class TestParseEventDictEdgeCases:
    def test_empty_markets_returns_none(self):
        event = {
            "title": "T", "slug": "s", "endDate": "2026-06-10T12:00:00Z",
            "description": "Celsius.", "markets": [],
        }
        assert parse_event_dict(event, "london", "2026-06-10", "highest") is None

    def test_missing_markets_key_returns_none(self):
        event = {"title": "T", "slug": "s", "endDate": "2026-06-10T12:00:00Z",
                 "description": "Celsius."}
        assert parse_event_dict(event, "london", "2026-06-10", "highest") is None
