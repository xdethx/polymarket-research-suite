"""Tests for weather_model/cities.py — city registry and lookup helpers."""
import pytest

from weather_model.cities import (
    ROUNDING_HKO,
    ROUNDING_WU,
    City,
    all_cities,
    get_city,
)


# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------

class TestRegistryCompleteness:
    # All 8 cities from W0 feasibility research, station table.
    EXPECTED = {
        "london", "paris", "new york", "houston",
        "shanghai", "tokyo", "incheon", "hong kong",
    }

    def test_all_target_cities_present(self):
        names = {c.name.lower() for c in all_cities()}
        missing = self.EXPECTED - names
        assert not missing, f"Missing target cities: {missing}"

    def test_all_cities_returns_list(self):
        result = all_cities()
        assert isinstance(result, list)
        assert len(result) >= 8

    def test_all_cities_are_city_instances(self):
        for c in all_cities():
            assert isinstance(c, City)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

class TestGetCity:
    def test_exact_case(self):
        c = get_city("London")
        assert c.name == "London"

    def test_lowercase(self):
        c = get_city("london")
        assert c.name == "London"

    def test_uppercase(self):
        c = get_city("LONDON")
        assert c.name == "London"

    def test_mixed_case(self):
        c = get_city("LoNdOn")
        assert c.name == "London"

    def test_leading_trailing_whitespace(self):
        c = get_city("  london  ")
        assert c.name == "London"

    def test_unknown_city_raises_key_error(self):
        with pytest.raises(KeyError):
            get_city("atlantis")

    def test_empty_string_raises_key_error(self):
        with pytest.raises(KeyError):
            get_city("")

    def test_result_is_frozen_dataclass(self):
        c = get_city("Tokyo")
        with pytest.raises(Exception):
            c.icao = "XXXX"  # frozen dataclass — mutation must raise


# ---------------------------------------------------------------------------
# ICAO codes — critical: wrong ICAO introduces systematic forecast error
# ---------------------------------------------------------------------------

class TestIcaoCodes:
    def test_london_eglc(self):
        assert get_city("london").icao == "EGLC"

    def test_paris_lfpb_not_cdg(self):
        c = get_city("paris")
        assert c.icao == "LFPB", (
            f"Paris must be Le Bourget (LFPB), got {c.icao}. "
            "CDG (LFPG) would introduce ~1-3°C forecast error."
        )
        assert c.icao != "LFPG", "Paris must NOT use CDG (LFPG)"

    def test_hong_kong_vhhh(self):
        assert get_city("hong kong").icao == "VHHH"

    def test_new_york_klga(self):
        assert get_city("new york").icao == "KLGA"

    def test_houston_khou(self):
        assert get_city("houston").icao == "KHOU"

    def test_shanghai_zspd(self):
        assert get_city("shanghai").icao == "ZSPD"

    def test_tokyo_rjtt(self):
        assert get_city("tokyo").icao == "RJTT"

    def test_incheon_rksi(self):
        assert get_city("incheon").icao == "RKSI"


# ---------------------------------------------------------------------------
# Rounding regimes
# ---------------------------------------------------------------------------

class TestRoundingRegimes:
    def test_london_wu_round(self):
        assert get_city("london").rounding_regime == ROUNDING_WU

    def test_hong_kong_hko_floor(self):
        assert get_city("hong kong").rounding_regime == ROUNDING_HKO

    def test_all_non_hk_cities_are_wu_round(self):
        for city in all_cities():
            if city.name.lower() == "hong kong":
                continue
            assert city.rounding_regime == ROUNDING_WU, (
                f"{city.name} should be {ROUNDING_WU}, "
                f"got {city.rounding_regime}"
            )

    def test_exactly_one_hko_floor_city(self):
        hko = [c for c in all_cities() if c.rounding_regime == ROUNDING_HKO]
        assert len(hko) == 1
        assert hko[0].name.lower() == "hong kong"


# ---------------------------------------------------------------------------
# Temperature units
# ---------------------------------------------------------------------------

class TestUnits:
    def test_celsius_cities(self):
        for name in ["london", "paris", "shanghai", "tokyo", "incheon", "hong kong"]:
            c = get_city(name)
            assert c.unit == "C", f"{name} should be °C, got {c.unit!r}"

    def test_fahrenheit_cities(self):
        for name in ["new york", "houston"]:
            c = get_city(name)
            assert c.unit == "F", f"{name} should be °F, got {c.unit!r}"

    def test_both_units_represented(self):
        units = {c.unit for c in all_cities()}
        assert "C" in units, "Registry must include at least one °C city"
        assert "F" in units, "Registry must include at least one °F city"


# ---------------------------------------------------------------------------
# Coordinates sanity
# ---------------------------------------------------------------------------

class TestCoordinates:
    def test_all_lats_in_range(self):
        for c in all_cities():
            assert -90.0 <= c.lat <= 90.0, f"{c.name} lat out of range: {c.lat}"

    def test_all_lons_in_range(self):
        for c in all_cities():
            assert -180.0 <= c.lon <= 180.0, f"{c.name} lon out of range: {c.lon}"

    def test_london_northern_hemisphere(self):
        c = get_city("london")
        assert c.lat > 50.0

    def test_hong_kong_southern_china(self):
        c = get_city("hong kong")
        assert 22.0 < c.lat < 23.0
        assert 113.0 < c.lon < 115.0
