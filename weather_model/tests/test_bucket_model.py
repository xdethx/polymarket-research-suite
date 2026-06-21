"""Tests for weather_model/bucket_model.py — Gaussian CDF bucket probability engine.

W1 acceptance check: all three W0 London June 10 worked-example probabilities
must reproduce within ±0.01 absolute tolerance:
    ECMWF μ=15.90, σ=0.92, regime=wu_round_half_up
    → 16°C P≈0.41,  15°C P≈0.27,  17°C P≈0.22
"""
import math
import pytest

from weather_model.bucket_model import (
    ROUNDING_HKO,
    ROUNDING_WU,
    Bucket,
    bucket_distribution,
    bucket_probability,
    make_ladder,
    normal_cdf,
)


# ---------------------------------------------------------------------------
# normal_cdf
# ---------------------------------------------------------------------------

class TestNormalCdf:
    def test_standard_normal_at_zero_is_half(self):
        assert abs(normal_cdf(0.0) - 0.5) < 1e-12

    def test_known_quantiles(self):
        # Φ(1) ≈ 0.84134, Φ(-1) ≈ 0.15866
        assert abs(normal_cdf(1.0) - 0.84134) < 1e-5
        assert abs(normal_cdf(-1.0) - 0.15866) < 1e-5
        # Φ(1.96) ≈ 0.97500
        assert abs(normal_cdf(1.96) - 0.97500) < 1e-4

    def test_symmetry(self):
        for x in [0.5, 1.0, 1.645, 1.96, 3.0]:
            assert abs(normal_cdf(x) + normal_cdf(-x) - 1.0) < 1e-12, (
                f"Symmetry violated at x={x}"
            )

    def test_non_standard_mean_shift(self):
        # Φ(16.0; 15.90, 0.92) should be slightly above 0.5
        p = normal_cdf(16.0, mu=15.90, sigma=0.92)
        assert 0.5 < p < 0.6

    def test_sigma_zero_below_mu(self):
        assert normal_cdf(4.9, mu=5.0, sigma=0.0) == 0.0

    def test_sigma_zero_at_mu(self):
        assert normal_cdf(5.0, mu=5.0, sigma=0.0) == 0.5

    def test_sigma_zero_above_mu(self):
        assert normal_cdf(5.1, mu=5.0, sigma=0.0) == 1.0

    def test_negative_sigma_raises(self):
        with pytest.raises(ValueError):
            normal_cdf(0.0, sigma=-0.1)

    def test_returns_in_unit_interval(self):
        for x in [-5.0, -1.0, 0.0, 1.0, 5.0]:
            p = normal_cdf(x)
            assert 0.0 <= p <= 1.0


# ---------------------------------------------------------------------------
# bucket_probability — WU round-half-up regime (W1 acceptance checks)
# ---------------------------------------------------------------------------

class TestBucketProbabilityWU:
    # W0 London June 10: ECMWF ensemble
    MU = 15.90
    SIGMA = 0.92
    REGIME = ROUNDING_WU

    def test_16c_approx_0_41(self):
        """W1 acceptance check: 16°C ≈ 0.41 (W0 worked example)."""
        p = bucket_probability(Bucket(16), self.MU, self.SIGMA, self.REGIME)
        assert abs(p - 0.41) < 0.01, (
            f"16°C: expected ≈0.41, got {p:.4f}. "
            "W0 acceptance check failed — verify normal_cdf or boundary logic."
        )

    def test_15c_approx_0_27(self):
        """W1 acceptance check: 15°C ≈ 0.27 (W0 worked example)."""
        p = bucket_probability(Bucket(15), self.MU, self.SIGMA, self.REGIME)
        assert abs(p - 0.27) < 0.01, (
            f"15°C: expected ≈0.27, got {p:.4f}."
        )

    def test_17c_approx_0_22(self):
        """W1 acceptance check: 17°C ≈ 0.22 (W0 worked example)."""
        p = bucket_probability(Bucket(17), self.MU, self.SIGMA, self.REGIME)
        assert abs(p - 0.22) < 0.01, (
            f"17°C: expected ≈0.22, got {p:.4f}."
        )

    def test_lower_tail_is_small_when_mu_is_high(self):
        # "13°C or below" with μ=15.9 — should be tiny
        p = bucket_probability(Bucket(13, is_lower_tail=True), self.MU, self.SIGMA, self.REGIME)
        assert 0.0 < p < 0.01, f"Lower-tail p should be near 0, got {p:.6f}"

    def test_upper_tail_is_tiny_when_mu_is_low(self):
        # "23°C or higher" with μ=15.9 — essentially 0
        p = bucket_probability(Bucket(23, is_upper_tail=True), self.MU, self.SIGMA, self.REGIME)
        assert 0.0 <= p < 0.001, f"Upper-tail p should be near 0, got {p:.6f}"

    def test_probabilities_in_unit_interval(self):
        for t in range(13, 24):
            b = Bucket(t)
            p = bucket_probability(b, self.MU, self.SIGMA, self.REGIME)
            assert 0.0 <= p <= 1.0, f"P out of range for {t}°C: {p}"

    def test_interior_boundary_formula(self):
        # Verify the formula directly: P(16) = Φ(16.5) - Φ(15.5)
        expected = normal_cdf(16.5, self.MU, self.SIGMA) - normal_cdf(15.5, self.MU, self.SIGMA)
        actual = bucket_probability(Bucket(16), self.MU, self.SIGMA, self.REGIME)
        assert abs(actual - expected) < 1e-12

    def test_lower_tail_formula(self):
        # P("13 or below") = Φ(13.5) for WU regime
        expected = normal_cdf(13.5, self.MU, self.SIGMA)
        actual = bucket_probability(Bucket(13, is_lower_tail=True), self.MU, self.SIGMA, self.REGIME)
        assert abs(actual - expected) < 1e-12

    def test_upper_tail_formula(self):
        # P("23 or higher") = 1 - Φ(22.5) for WU regime
        expected = 1.0 - normal_cdf(22.5, self.MU, self.SIGMA)
        actual = bucket_probability(Bucket(23, is_upper_tail=True), self.MU, self.SIGMA, self.REGIME)
        assert abs(actual - expected) < 1e-12


# ---------------------------------------------------------------------------
# bucket_probability — HKO floor regime
# ---------------------------------------------------------------------------

class TestBucketProbabilityHKO:
    MU = 28.5
    SIGMA = 1.0
    REGIME = ROUNDING_HKO

    def test_interior_bucket_28_is_substantial(self):
        # HKO floor: bucket 28 covers [28.0, 29.0), and μ=28.5 is inside
        p = bucket_probability(Bucket(28), self.MU, self.SIGMA, self.REGIME)
        assert p > 0.30, f"Bucket 28 [28,29) with μ=28.5 should dominate: got {p:.4f}"

    def test_hko_interior_formula(self):
        # P(28) = Φ(29.0) - Φ(28.0) for HKO regime
        expected = normal_cdf(29.0, self.MU, self.SIGMA) - normal_cdf(28.0, self.MU, self.SIGMA)
        actual = bucket_probability(Bucket(28), self.MU, self.SIGMA, self.REGIME)
        assert abs(actual - expected) < 1e-12

    def test_hko_lower_tail_formula(self):
        # P("24 or below") = Φ(25.0) for HKO regime
        expected = normal_cdf(25.0, self.MU, self.SIGMA)
        actual = bucket_probability(Bucket(24, is_lower_tail=True), self.MU, self.SIGMA, self.REGIME)
        assert abs(actual - expected) < 1e-12

    def test_hko_upper_tail_formula(self):
        # P("33 or higher") = 1 - Φ(33.0) for HKO regime
        expected = 1.0 - normal_cdf(33.0, self.MU, self.SIGMA)
        actual = bucket_probability(Bucket(33, is_upper_tail=True), self.MU, self.SIGMA, self.REGIME)
        assert abs(actual - expected) < 1e-12

    def test_hko_vs_wu_differ_at_boundary(self):
        # With a mean right at N+0.5, WU and HKO should give different probabilities
        # for bucket N, since WU covers [N-0.5, N+0.5) and HKO covers [N, N+1).
        mu_mid = 28.5  # exactly at HKO bucket 28's center vs WU bucket 28.5 boundary
        p_wu = bucket_probability(Bucket(28), mu_mid, 1.0, ROUNDING_WU)
        p_hko = bucket_probability(Bucket(28), mu_mid, 1.0, ROUNDING_HKO)
        assert abs(p_wu - p_hko) > 0.01, (
            "WU and HKO regimes should differ at N+0.5 — check boundary logic"
        )


# ---------------------------------------------------------------------------
# bucket_distribution — sums to ~1 over a complete ladder
# ---------------------------------------------------------------------------

class TestBucketDistribution:
    def test_sums_to_one_wu(self):
        ladder = make_ladder(list(range(13, 24)))
        dist = bucket_distribution(ladder, mu=15.90, sigma=0.92, regime=ROUNDING_WU)
        total = sum(dist.values())
        assert abs(total - 1.0) < 1e-10, f"WU ladder sum = {total}"

    def test_sums_to_one_hko(self):
        ladder = make_ladder(list(range(24, 35)))
        dist = bucket_distribution(ladder, mu=28.5, sigma=1.0, regime=ROUNDING_HKO)
        total = sum(dist.values())
        assert abs(total - 1.0) < 1e-10, f"HKO ladder sum = {total}"

    def test_w0_acceptance_via_distribution(self):
        """W0 worked example must reproduce through bucket_distribution."""
        ladder = make_ladder(list(range(13, 24)))
        dist = bucket_distribution(ladder, mu=15.90, sigma=0.92, regime=ROUNDING_WU)
        p16 = dist[Bucket(16)]
        p15 = dist[Bucket(15)]
        p17 = dist[Bucket(17)]
        assert abs(p16 - 0.41) < 0.01, f"16°C: expected ≈0.41, got {p16:.4f}"
        assert abs(p15 - 0.27) < 0.01, f"15°C: expected ≈0.27, got {p15:.4f}"
        assert abs(p17 - 0.22) < 0.01, f"17°C: expected ≈0.22, got {p17:.4f}"

    def test_all_probabilities_nonnegative(self):
        ladder = make_ladder(list(range(13, 24)))
        dist = bucket_distribution(ladder, mu=15.90, sigma=0.92, regime=ROUNDING_WU)
        for b, p in dist.items():
            assert p >= 0.0, f"Negative probability for bucket {b.value}: {p}"

    def test_sigma_zero_point_mass_at_16(self):
        # With σ=0 and μ=16.0, all probability should collapse onto bucket 16.
        ladder = make_ladder(list(range(13, 24)))
        dist = bucket_distribution(ladder, mu=16.0, sigma=0.0, regime=ROUNDING_WU)
        total = sum(dist.values())
        assert abs(total - 1.0) < 1e-10, f"σ=0 total = {total}"
        p16 = dist[Bucket(16)]
        assert abs(p16 - 1.0) < 1e-10, (
            f"σ=0 point mass at μ=16.0 should be at bucket 16 (P=1.0), got {p16}"
        )

    def test_sigma_zero_point_mass_at_boundary(self):
        # μ=16.5 is exactly at the WU boundary between 16 and 17.
        # normal_cdf(16.5; 16.5, 0) = 0.5, so P(16) = Φ(16.5)-Φ(15.5) = 0.5-0.0 = 0.5
        # and P(17) = Φ(17.5)-Φ(16.5) = 1.0-0.5 = 0.5
        ladder = make_ladder(list(range(13, 24)))
        dist = bucket_distribution(ladder, mu=16.5, sigma=0.0, regime=ROUNDING_WU)
        total = sum(dist.values())
        assert abs(total - 1.0) < 1e-10
        p16 = dist[Bucket(16)]
        p17 = dist[Bucket(17)]
        assert abs(p16 - 0.5) < 1e-10
        assert abs(p17 - 0.5) < 1e-10

    def test_unknown_regime_raises(self):
        with pytest.raises(ValueError):
            bucket_probability(Bucket(16), 15.90, 0.92, "unknown_regime")


# ---------------------------------------------------------------------------
# make_ladder
# ---------------------------------------------------------------------------

class TestMakeLadder:
    def test_lowest_is_lower_tail(self):
        ladder = make_ladder([13, 14, 15, 16])
        assert ladder[0].value == 13
        assert ladder[0].is_lower_tail

    def test_highest_is_upper_tail(self):
        ladder = make_ladder([13, 14, 15, 16])
        assert ladder[-1].value == 16
        assert ladder[-1].is_upper_tail

    def test_interior_buckets_are_plain(self):
        ladder = make_ladder([13, 14, 15, 16])
        for b in ladder[1:-1]:
            assert not b.is_lower_tail
            assert not b.is_upper_tail

    def test_two_element_ladder(self):
        ladder = make_ladder([15, 20])
        assert ladder[0].is_lower_tail
        assert ladder[1].is_upper_tail
        assert len(ladder) == 2

    def test_unsorted_input_sorted(self):
        ladder = make_ladder([20, 13, 16, 14])
        values = [b.value for b in ladder]
        assert values == sorted(values)

    def test_duplicate_input_deduplicated(self):
        ladder = make_ladder([13, 14, 14, 15])
        values = [b.value for b in ladder]
        assert len(values) == len(set(values))

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            make_ladder([])
