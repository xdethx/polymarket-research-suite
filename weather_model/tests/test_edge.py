"""Tests for weather_model/edge.py — edge calculation, Kelly sizing, and gatekeeping."""
import pytest

from weather_model.edge import (
    DEFAULT_KELLY_FRACTION,
    devig,
    kelly_size,
    min_edge_gate,
    no_edge,
    yes_edge,
)


# ---------------------------------------------------------------------------
# devig
# ---------------------------------------------------------------------------

class TestDevig:
    def test_w0_london_ladder_normalizes_to_one(self):
        """W0 London ladder (~4% overround, sum≈1.040) normalizes to 1.0."""
        raw = [0.003, 0.020, 0.090, 0.370, 0.360, 0.170, 0.030, 0.006, 0.005, 0.001, 0.001]
        normalized = devig(raw)
        assert abs(sum(normalized) - 1.0) < 1e-10

    def test_already_normalized_unchanged(self):
        prices = [0.3, 0.4, 0.3]
        out = devig(prices)
        assert abs(sum(out) - 1.0) < 1e-10
        for orig, norm in zip(prices, out):
            assert abs(orig - norm) < 1e-10

    def test_proportional_scaling(self):
        # sum=1.1 → each value divided by 1.1
        raw = [0.5, 0.6]
        out = devig(raw)
        assert abs(out[0] - 0.5 / 1.1) < 1e-10
        assert abs(out[1] - 0.6 / 1.1) < 1e-10

    def test_single_price(self):
        out = devig([0.7])
        assert abs(out[0] - 1.0) < 1e-10

    def test_relative_order_preserved(self):
        raw = [0.1, 0.5, 0.4, 0.1]
        out = devig(raw)
        # De-vigged values should maintain the same relative ordering
        assert out[0] < out[1]
        assert out[2] < out[1]

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            devig([])

    def test_zero_sum_raises(self):
        with pytest.raises(ValueError):
            devig([0.0, 0.0])

    def test_negative_sum_raises(self):
        with pytest.raises(ValueError):
            devig([-0.5, -0.5])


# ---------------------------------------------------------------------------
# yes_edge and no_edge
# ---------------------------------------------------------------------------

class TestEdge:
    def test_yes_edge_positive_when_underpriced(self):
        # Model 0.41, market 0.365 → YES underpriced → positive YES edge
        e = yes_edge(0.41, 0.365)
        assert e > 0.0
        assert abs(e - (0.41 - 0.365)) < 1e-10

    def test_yes_edge_negative_when_overpriced(self):
        # Model 0.07, market 0.167 → YES overpriced → negative YES edge
        e = yes_edge(0.07, 0.167)
        assert e < 0.0

    def test_yes_edge_zero_when_fair(self):
        assert abs(yes_edge(0.5, 0.5)) < 1e-10

    def test_no_edge_positive_when_yes_overpriced(self):
        # Model 0.07, market 0.167 → YES overpriced → buy NO → positive NO edge
        e = no_edge(0.07, 0.167)
        assert e > 0.0
        assert abs(e - (0.167 - 0.07)) < 1e-10

    def test_no_edge_negative_when_yes_underpriced(self):
        e = no_edge(0.41, 0.365)
        assert e < 0.0

    def test_no_edge_zero_when_fair(self):
        assert abs(no_edge(0.5, 0.5)) < 1e-10

    def test_yes_and_no_edge_are_negatives(self):
        """yes_edge + no_edge must always sum to 0 (they're negatives of each other)."""
        for bp, mp in [(0.4, 0.35), (0.1, 0.2), (0.5, 0.5), (0.8, 0.7)]:
            s = yes_edge(bp, mp) + no_edge(bp, mp)
            assert abs(s) < 1e-10, (
                f"yes+no edge != 0 for bp={bp}, mp={mp}: sum={s}"
            )

    def test_yes_edge_sign_covers_w0_example(self):
        # W0: 15°C bucket — ECMWF P≈0.27, market P≈0.089 → strong YES edge
        e = yes_edge(0.27, 0.089)
        assert e > 0.15  # roughly 0.18 per W0 table

    def test_no_edge_sign_covers_w0_gfs_example(self):
        # W0: 15°C bucket — GFS P≈0.01, market P≈0.089 → GFS says overpriced → NO edge
        e = no_edge(0.01, 0.089)
        assert e > 0.0
        assert abs(e - 0.079) < 0.002


# ---------------------------------------------------------------------------
# kelly_size
# ---------------------------------------------------------------------------

class TestKellySize:
    def test_yes_zero_edge_returns_zero(self):
        size = kelly_size(0.4, 0.4, 0.25, 1000.0, "YES")
        assert size == 0.0

    def test_no_zero_edge_returns_zero(self):
        size = kelly_size(0.4, 0.4, 0.25, 1000.0, "NO")
        assert size == 0.0

    def test_yes_positive_edge_positive_size(self):
        size = kelly_size(0.50, 0.40, 0.25, 1000.0, "YES")
        assert size > 0.0

    def test_no_positive_edge_positive_size(self):
        size = kelly_size(0.30, 0.40, 0.25, 1000.0, "NO")
        assert size > 0.0

    def test_yes_size_formula(self):
        # f* = (0.50 - 0.40) / (1 - 0.40) = 0.10/0.60 ≈ 0.1667
        # size = 1000 * 0.25 * 0.1667 ≈ 41.67
        expected_f = (0.50 - 0.40) / (1.0 - 0.40)
        expected_size = 1000.0 * 0.25 * expected_f
        size = kelly_size(0.50, 0.40, 0.25, 1000.0, "YES")
        assert abs(size - expected_size) < 1e-10

    def test_no_size_formula(self):
        # f* = (0.40 - 0.30) / 0.40 = 0.25
        # size = 1000 * 0.25 * 0.25 = 62.5
        expected_f = (0.40 - 0.30) / 0.40
        expected_size = 1000.0 * 0.25 * expected_f
        size = kelly_size(0.30, 0.40, 0.25, 1000.0, "NO")
        assert abs(size - expected_size) < 1e-10

    def test_yes_monotonic_in_edge(self):
        """Larger YES edge → strictly larger size."""
        sizes = [
            kelly_size(bucket_prob, 0.40, 0.25, 1000.0, "YES")
            for bucket_prob in [0.41, 0.45, 0.50, 0.60, 0.70]
        ]
        for a, b in zip(sizes, sizes[1:]):
            assert b > a, f"Kelly YES sizes not monotone: {sizes}"

    def test_no_monotonic_in_edge(self):
        """Larger NO edge → strictly larger size."""
        sizes = [
            kelly_size(bucket_prob, 0.60, 0.25, 1000.0, "NO")
            for bucket_prob in [0.59, 0.55, 0.50, 0.40, 0.30]
        ]
        for a, b in zip(sizes, sizes[1:]):
            assert b > a, f"Kelly NO sizes not monotone: {sizes}"

    def test_scales_linearly_with_bankroll(self):
        s1 = kelly_size(0.50, 0.40, 0.25, 1000.0, "YES")
        s2 = kelly_size(0.50, 0.40, 0.25, 2000.0, "YES")
        assert abs(s2 - 2 * s1) < 1e-10

    def test_scales_linearly_with_kelly_fraction(self):
        s1 = kelly_size(0.50, 0.40, 0.25, 1000.0, "YES")
        s2 = kelly_size(0.50, 0.40, 0.50, 1000.0, "YES")
        assert abs(s2 - 2 * s1) < 1e-10

    def test_case_insensitive_side(self):
        s_yes_upper = kelly_size(0.50, 0.40, 0.25, 1000.0, "YES")
        s_yes_lower = kelly_size(0.50, 0.40, 0.25, 1000.0, "yes")
        assert abs(s_yes_upper - s_yes_lower) < 1e-10

    def test_zero_bankroll_returns_zero(self):
        assert kelly_size(0.50, 0.40, 0.25, 0.0, "YES") == 0.0

    def test_invalid_price_zero_raises(self):
        with pytest.raises(ValueError):
            kelly_size(0.5, 0.0, 0.25, 1000.0, "YES")

    def test_invalid_price_one_raises(self):
        with pytest.raises(ValueError):
            kelly_size(0.5, 1.0, 0.25, 1000.0, "YES")

    def test_invalid_price_above_one_raises(self):
        with pytest.raises(ValueError):
            kelly_size(0.5, 1.1, 0.25, 1000.0, "YES")

    def test_negative_kelly_fraction_raises(self):
        with pytest.raises(ValueError):
            kelly_size(0.5, 0.4, -0.25, 1000.0, "YES")

    def test_zero_kelly_fraction_raises(self):
        with pytest.raises(ValueError):
            kelly_size(0.5, 0.4, 0.0, 1000.0, "YES")

    def test_negative_bankroll_raises(self):
        with pytest.raises(ValueError):
            kelly_size(0.5, 0.4, 0.25, -1.0, "YES")

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError):
            kelly_size(0.5, 0.4, 0.25, 1000.0, "BUY")

    def test_default_kelly_fraction_is_conservative(self):
        assert 0.0 < DEFAULT_KELLY_FRACTION <= 0.25


# ---------------------------------------------------------------------------
# min_edge_gate
# ---------------------------------------------------------------------------

class TestMinEdgeGate:
    def test_above_threshold_passes(self):
        assert min_edge_gate(0.10, 0.05) is True

    def test_at_threshold_passes(self):
        assert min_edge_gate(0.05, 0.05) is True

    def test_below_threshold_blocked(self):
        assert min_edge_gate(0.03, 0.05) is False

    def test_zero_edge_blocked_for_nonzero_threshold(self):
        assert min_edge_gate(0.0, 0.01) is False

    def test_negative_edge_uses_absolute_value(self):
        # |−0.10| = 0.10 >= 0.05 → passes
        assert min_edge_gate(-0.10, 0.05) is True

    def test_small_negative_edge_blocked(self):
        # |−0.03| = 0.03 < 0.05 → blocked
        assert min_edge_gate(-0.03, 0.05) is False

    def test_zero_threshold_always_passes(self):
        # Every edge ≥ 0 when threshold=0
        for e in [0.0, 0.001, 0.1, -0.1]:
            assert min_edge_gate(e, 0.0) is True
