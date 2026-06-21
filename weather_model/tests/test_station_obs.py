"""Tests for weather_model/station_obs.py — pure functions only, no network."""
import datetime

import pytest

from weather_model.station_obs import local_day_utc_window, round_half_up


# ---------------------------------------------------------------------------
# round_half_up — WU whole-degree rounding
# ---------------------------------------------------------------------------


class TestRoundHalfUp:
    def test_exact_integer(self):
        assert round_half_up(20.0) == 20

    def test_rounds_up_at_half(self):
        assert round_half_up(20.5) == 21

    def test_rounds_down_below_half(self):
        assert round_half_up(20.49) == 20

    def test_rounds_up_above_half(self):
        assert round_half_up(20.51) == 21

    def test_negative_temperature(self):
        assert round_half_up(-0.5) == 0   # floor(-0.5 + 0.5) = floor(0) = 0

    def test_negative_round_down(self):
        # -1.6 + 0.5 = -1.1; floor(-1.1) = -2
        assert round_half_up(-1.6) == -2

    def test_fixture_value_tokyo(self):
        # W2.5 claimed Tokyo realized 20.2 → bucket 20
        assert round_half_up(20.2) == 20

    def test_fixture_value_metar_tokyo(self):
        # METAR measured 22.0 → bucket 22
        assert round_half_up(22.0) == 22

    def test_fixture_value_seoul_w25(self):
        # W2.5 claimed Seoul realized 20.6 → bucket 21
        assert round_half_up(20.6) == 21

    def test_fixture_value_shanghai_metar(self):
        # METAR Shanghai 25.0 → bucket 25
        assert round_half_up(25.0) == 25

    def test_boundary_just_below_half(self):
        # 22.499 → still 22, not 23
        assert round_half_up(22.499) == 22

    def test_boundary_just_above_half(self):
        # 22.500 → 23 (half rounds up)
        assert round_half_up(22.500) == 23


# ---------------------------------------------------------------------------
# local_day_utc_window — station-local calendar day → UTC window
# ---------------------------------------------------------------------------


_UTC = datetime.timezone.utc


class TestLocalDayUtcWindow:
    def test_tokyo_june9(self):
        """Tokyo (UTC+9): local June 9 = UTC June 8 15:00 → June 9 15:00."""
        start, end = local_day_utc_window("2026-06-09", 9)
        assert start == datetime.datetime(2026, 6, 8, 15, 0, tzinfo=_UTC)
        assert end == datetime.datetime(2026, 6, 9, 15, 0, tzinfo=_UTC)

    def test_seoul_june9(self):
        """Seoul (UTC+9): same as Tokyo."""
        start, end = local_day_utc_window("2026-06-09", 9)
        assert start == datetime.datetime(2026, 6, 8, 15, 0, tzinfo=_UTC)
        assert end == datetime.datetime(2026, 6, 9, 15, 0, tzinfo=_UTC)

    def test_shanghai_june9(self):
        """Shanghai (UTC+8): local June 9 = UTC June 8 16:00 → June 9 16:00."""
        start, end = local_day_utc_window("2026-06-09", 8)
        assert start == datetime.datetime(2026, 6, 8, 16, 0, tzinfo=_UTC)
        assert end == datetime.datetime(2026, 6, 9, 16, 0, tzinfo=_UTC)

    def test_london_bst_june9(self):
        """London BST (UTC+1): local June 9 = UTC June 8 23:00 → June 9 23:00."""
        start, end = local_day_utc_window("2026-06-09", 1)
        assert start == datetime.datetime(2026, 6, 8, 23, 0, tzinfo=_UTC)
        assert end == datetime.datetime(2026, 6, 9, 23, 0, tzinfo=_UTC)

    def test_utc_zero_offset(self):
        """UTC±0: window is exactly the calendar day in UTC."""
        start, end = local_day_utc_window("2026-06-09", 0)
        assert start == datetime.datetime(2026, 6, 9, 0, 0, tzinfo=_UTC)
        assert end == datetime.datetime(2026, 6, 10, 0, 0, tzinfo=_UTC)

    def test_window_is_exactly_24h(self):
        start, end = local_day_utc_window("2026-06-09", 9)
        assert (end - start).total_seconds() == 86400.0

    def test_returns_utc_aware_datetimes(self):
        start, end = local_day_utc_window("2026-06-09", 9)
        assert start.tzinfo is not None
        assert end.tzinfo is not None
        assert start.utcoffset() == datetime.timedelta(0)

    def test_snapshot_at_18utc_is_after_tokyo_window_end(self):
        """At 18:00 UTC, Tokyo's local June-9 window (ends 15:00 UTC) is over."""
        _, utc_end = local_day_utc_window("2026-06-09", 9)
        snapshot = datetime.datetime(2026, 6, 9, 18, 0, tzinfo=_UTC)
        assert snapshot >= utc_end  # day is complete

    def test_snapshot_at_18utc_is_after_shanghai_window_end(self):
        """At 18:00 UTC, Shanghai's local June-9 window (ends 16:00 UTC) is over."""
        _, utc_end = local_day_utc_window("2026-06-09", 8)
        snapshot = datetime.datetime(2026, 6, 9, 18, 0, tzinfo=_UTC)
        assert snapshot >= utc_end  # day is complete

    def test_snapshot_at_18utc_is_before_london_window_end(self):
        """At 18:00 UTC, London's local June-9 window (ends 23:00 UTC) is NOT over."""
        _, utc_end = local_day_utc_window("2026-06-09", 1)
        snapshot = datetime.datetime(2026, 6, 9, 18, 0, tzinfo=_UTC)
        assert snapshot < utc_end  # day still in progress
