"""Tests for weather_model/forecast_fetcher.py — offline only; no live network."""
import datetime as _dt
import json
import os

import pytest

import weather_model.forecast_fetcher as ff
from weather_model.forecast_fetcher import (
    c_to_f,
    clear_cache,
    fetch_ensemble,
    members_to_mu_sigma,
)


class _FakeDateBefore20260610:
    """Stand-in for datetime.date that returns 2026-06-09 as today.

    Used to prevent fetch_ensemble's ``delta < 0`` guard from tripping when
    the fixture dates (2026-06-10/11) are now in the past.
    """

    @classmethod
    def today(cls) -> _dt.date:
        return _dt.date(2026, 6, 9)

    @classmethod
    def fromisoformat(cls, s: str) -> _dt.date:
        return _dt.date.fromisoformat(s)

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name: str):
    with open(os.path.join(_FIXTURE_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# c_to_f — unit conversion
# ---------------------------------------------------------------------------


class TestCToF:
    def test_freezing_point(self):
        assert abs(c_to_f(0.0) - 32.0) < 1e-9

    def test_boiling_point(self):
        assert abs(c_to_f(100.0) - 212.0) < 1e-9

    def test_minus_40_equal_in_both(self):
        assert abs(c_to_f(-40.0) - (-40.0)) < 1e-9

    def test_body_temperature(self):
        # 37°C ≈ 98.6°F
        assert abs(c_to_f(37.0) - 98.6) < 0.01

    def test_negative_celsius(self):
        # -10°C = 14°F
        assert abs(c_to_f(-10.0) - 14.0) < 1e-9


# ---------------------------------------------------------------------------
# members_to_mu_sigma
# ---------------------------------------------------------------------------


class TestMembersToMuSigma:
    def test_mean_correct(self):
        members = [10.0, 12.0, 14.0]
        mu, _ = members_to_mu_sigma(members)
        assert abs(mu - 12.0) < 1e-9

    def test_sigma_positive(self):
        members = [10.0, 12.0, 14.0]
        _, sigma = members_to_mu_sigma(members)
        assert sigma > 0.0

    def test_single_member_sigma_clamped(self):
        # Single member → sigma clamped to 0.01
        _, sigma = members_to_mu_sigma([15.0])
        assert sigma == 0.01

    def test_all_equal_members_sigma_clamped(self):
        # All members equal → pstdev = 0 → clamped to 0.01
        _, sigma = members_to_mu_sigma([15.0, 15.0, 15.0])
        assert sigma == 0.01

    def test_empty_raises(self):
        with pytest.raises((ValueError, Exception)):
            members_to_mu_sigma([])


# ---------------------------------------------------------------------------
# _parse_members (internal, tested indirectly via fetch_ensemble mock)
# ---------------------------------------------------------------------------


class TestParseMembers:
    """Test _parse_members via mocked fetch_ensemble."""

    def test_correct_members_for_date(self, monkeypatch):
        fixture = _load("ensemble_ecmwf_sample.json")

        def mock_http(url, params=None):
            return fixture

        monkeypatch.setattr(ff, "_http_get_json", mock_http)
        monkeypatch.setattr(ff, "date_type", _FakeDateBefore20260610)
        clear_cache()

        members = fetch_ensemble(51.5, 0.05, "2026-06-10", "ecmwf_ifs025")
        assert members is not None
        # Fixture has control + member01 + member02 + member03 = 4 values
        assert len(members) == 4
        # Control is 15.8, member01=15.2, member02=16.4, member03=15.6
        assert abs(members[0] - 15.8) < 0.001
        assert abs(members[1] - 15.2) < 0.001

    def test_date_not_in_response_returns_none(self, monkeypatch):
        fixture = _load("ensemble_ecmwf_sample.json")

        def mock_http(url, params=None):
            return fixture

        monkeypatch.setattr(ff, "_http_get_json", mock_http)
        clear_cache()

        # "2099-01-01" is not in the fixture's time array
        members = fetch_ensemble(51.5, 0.05, "2099-01-01", "ecmwf_ifs025")
        assert members is None

    def test_unit_conversion_to_fahrenheit(self, monkeypatch):
        fixture = _load("ensemble_ecmwf_sample.json")

        def mock_http(url, params=None):
            return fixture

        monkeypatch.setattr(ff, "_http_get_json", mock_http)
        monkeypatch.setattr(ff, "date_type", _FakeDateBefore20260610)
        clear_cache()

        members_c = fetch_ensemble(51.5, 0.05, "2026-06-10", "ecmwf_ifs025", unit="C")
        clear_cache()
        members_f = fetch_ensemble(51.5, 0.05, "2026-06-10", "ecmwf_ifs025", unit="F")

        assert members_c is not None
        assert members_f is not None
        # Each °F value should be c_to_f of corresponding °C value
        for mc, mf in zip(members_c, members_f):
            assert abs(mf - c_to_f(mc)) < 1e-6


# ---------------------------------------------------------------------------
# Cache: same object returned on second call
# ---------------------------------------------------------------------------


class TestCacheIdentity:
    def test_cache_returns_same_object(self, monkeypatch):
        fixture = _load("ensemble_ecmwf_sample.json")
        call_count = [0]

        def mock_http(url, params=None):
            call_count[0] += 1
            return fixture

        monkeypatch.setattr(ff, "_http_get_json", mock_http)
        monkeypatch.setattr(ff, "date_type", _FakeDateBefore20260610)
        clear_cache()

        r1 = fetch_ensemble(51.5, 0.05, "2026-06-10", "ecmwf_ifs025")
        r2 = fetch_ensemble(51.5, 0.05, "2026-06-10", "ecmwf_ifs025")

        assert r1 is r2            # exact same object (not just equal)
        assert call_count[0] == 1  # HTTP only called once

    def test_different_unit_not_shared(self, monkeypatch):
        """°C and °F results have different cache keys."""
        fixture = _load("ensemble_ecmwf_sample.json")

        def mock_http(url, params=None):
            return fixture

        monkeypatch.setattr(ff, "_http_get_json", mock_http)
        monkeypatch.setattr(ff, "date_type", _FakeDateBefore20260610)
        clear_cache()

        r_c = fetch_ensemble(51.5, 0.05, "2026-06-10", "ecmwf_ifs025", unit="C")
        clear_cache()
        r_f = fetch_ensemble(51.5, 0.05, "2026-06-10", "ecmwf_ifs025", unit="F")
        assert r_c is not r_f


# ---------------------------------------------------------------------------
# Graceful failure
# ---------------------------------------------------------------------------


class TestGracefulFailure:
    def test_returns_none_on_http_exception(self, monkeypatch):
        # The real _http_get_json catches all exceptions and returns None.
        # Test that when _http_get_json returns None (as it does on any error),
        # fetch_ensemble propagates None to the caller.
        monkeypatch.setattr(ff, "_http_get_json", lambda *a, **k: None)
        clear_cache()

        result = fetch_ensemble(51.5, 0.05, "2026-06-10", "ecmwf_ifs025")
        assert result is None

    def test_returns_none_on_none_response(self, monkeypatch):
        monkeypatch.setattr(ff, "_http_get_json", lambda *a, **k: None)
        clear_cache()

        result = fetch_ensemble(51.5, 0.05, "2026-06-10", "ecmwf_ifs025")
        assert result is None

    def test_past_date_returns_none(self, monkeypatch):
        # delta < 0 → short-circuit before HTTP
        monkeypatch.setattr(ff, "_http_get_json", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not be called")))
        clear_cache()

        result = fetch_ensemble(51.5, 0.05, "2000-01-01", "ecmwf_ifs025")
        assert result is None
