"""Tests for weather_model/blend.py — multi-model forecast blending.

Core safety property: when model means disagree, sigma_blend must exceed
every individual sigma.  This prevents the W0 fake-edge artifact.
"""
import math
import pytest

from weather_model.blend import blend_models


# ---------------------------------------------------------------------------
# W0 acceptance checks
# ---------------------------------------------------------------------------

class TestW0AcceptanceCheck:
    """The W0 ECMWF+GFS disagreement example is the primary guardrail test."""

    ECMWF = (15.90, 0.92)  # (mu, sigma)
    GFS   = (18.15, 0.73)
    EQUAL_MU_BLEND = (15.90 + 18.15) / 2.0  # 17.025

    def test_sigma_exceeds_ecmwf(self):
        """Blended σ must exceed ECMWF σ when models disagree."""
        _, sigma_b = blend_models([self.ECMWF, self.GFS])
        assert sigma_b > 0.92, (
            f"sigma_blend={sigma_b:.4f} must exceed ECMWF sigma=0.92. "
            "Safety property violated — between-model variance not added."
        )

    def test_sigma_exceeds_gfs(self):
        """Blended σ must exceed GFS σ when models disagree."""
        _, sigma_b = blend_models([self.ECMWF, self.GFS])
        assert sigma_b > 0.73, (
            f"sigma_blend={sigma_b:.4f} must exceed GFS sigma=0.73."
        )

    def test_mean_between_models(self):
        """Equal-weight blended mean must lie strictly between the two model means."""
        mu_b, _ = blend_models([self.ECMWF, self.GFS])
        assert 15.90 < mu_b < 18.15, (
            f"mu_blend={mu_b:.4f} must be in (15.90, 18.15)"
        )

    def test_mean_equals_arithmetic_mean(self):
        """With equal weights, blended mean = simple arithmetic mean."""
        mu_b, _ = blend_models([self.ECMWF, self.GFS])
        assert abs(mu_b - self.EQUAL_MU_BLEND) < 1e-10

    def test_sigma_value_matches_law_of_total_variance(self):
        """Verify the numerical result of the law-of-total-variance formula."""
        mu_b = self.EQUAL_MU_BLEND
        # within-model variance: 0.5*(0.92² + 0.73²)
        within = 0.5 * (0.92 ** 2 + 0.73 ** 2)
        # between-model variance: 0.5*(15.90 - 17.025)² + 0.5*(18.15 - 17.025)²
        between = (
            0.5 * (15.90 - mu_b) ** 2
            + 0.5 * (18.15 - mu_b) ** 2
        )
        expected_sigma = math.sqrt(within + between)
        _, sigma_b = blend_models([self.ECMWF, self.GFS])
        assert abs(sigma_b - expected_sigma) < 1e-10


# ---------------------------------------------------------------------------
# Identity and edge cases
# ---------------------------------------------------------------------------

class TestBlendIdentity:
    def test_single_model_identity(self):
        """Single model: output equals input (no modification)."""
        mu_b, sigma_b = blend_models([(15.90, 0.92)])
        assert abs(mu_b - 15.90) < 1e-10
        assert abs(sigma_b - 0.92) < 1e-10

    def test_identical_models_no_between_variance(self):
        """Two identical models: sigma_blend == sigma (zero between-model variance)."""
        mu_b, sigma_b = blend_models([(16.0, 1.0), (16.0, 1.0)])
        assert abs(mu_b - 16.0) < 1e-10
        assert abs(sigma_b - 1.0) < 1e-10, (
            f"Identical models must not inflate sigma; got {sigma_b:.6f}"
        )

    def test_three_models_equal_weight_mean(self):
        forecasts = [(14.0, 1.0), (16.0, 1.0), (18.0, 1.0)]
        mu_b, _ = blend_models(forecasts)
        assert abs(mu_b - 16.0) < 1e-10

    def test_three_models_sigma_inflated(self):
        """Three models with spread means: sigma_blend > 1.0."""
        forecasts = [(14.0, 1.0), (16.0, 1.0), (18.0, 1.0)]
        _, sigma_b = blend_models(forecasts)
        assert sigma_b > 1.0

    def test_symmetric_disagreement_centered_mean(self):
        """Models equally offset above and below: mean is the center."""
        mu_b, _ = blend_models([(10.0, 1.0), (20.0, 1.0)])
        assert abs(mu_b - 15.0) < 1e-10


# ---------------------------------------------------------------------------
# Inverse-MAE weighting hook
# ---------------------------------------------------------------------------

class TestMaeWeighting:
    ECMWF = (15.90, 0.92)
    GFS   = (18.15, 0.73)

    def test_lower_mae_gets_higher_weight(self):
        """Model with lower MAE (more accurate) should pull the mean toward it."""
        # ECMWF MAE=0.5 (more accurate), GFS MAE=2.0 → mean pulled toward ECMWF
        mu_b, _ = blend_models([self.ECMWF, self.GFS], maes=[0.5, 2.0])
        equal_mu = (15.90 + 18.15) / 2.0  # 17.025
        assert mu_b < equal_mu, (
            f"MAE-weighted mu {mu_b:.4f} should favor ECMWF (lower MAE). "
            f"Equal-weight mean is {equal_mu:.4f}."
        )
        # Still between the two models
        assert 15.90 < mu_b < 18.15

    def test_equal_maes_matches_equal_weights(self):
        """Equal MAEs should produce the same result as equal weights."""
        mu_equal, sigma_equal = blend_models([self.ECMWF, self.GFS])
        mu_mae, sigma_mae = blend_models([self.ECMWF, self.GFS], maes=[1.0, 1.0])
        assert abs(mu_equal - mu_mae) < 1e-10
        assert abs(sigma_equal - sigma_mae) < 1e-10

    def test_extreme_mae_weight_approaches_single_model(self):
        """Very high MAE on one model → blended result approaches the other model."""
        # GFS MAE extremely large → effectively only ECMWF
        mu_b, sigma_b = blend_models([self.ECMWF, self.GFS], maes=[1.0, 1e9])
        assert abs(mu_b - 15.90) < 0.01, (
            f"With extreme GFS MAE, mean should approach ECMWF mu=15.90, got {mu_b:.4f}"
        )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestBlendValidation:
    def test_empty_forecasts_raises(self):
        with pytest.raises(ValueError, match="empty"):
            blend_models([])

    def test_zero_sigma_raises(self):
        with pytest.raises(ValueError):
            blend_models([(15.90, 0.0), (18.15, 0.73)])

    def test_negative_sigma_raises(self):
        with pytest.raises(ValueError):
            blend_models([(15.90, -0.5)])

    def test_maes_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            blend_models([(15.90, 0.92), (18.15, 0.73)], maes=[0.5])

    def test_zero_mae_raises(self):
        with pytest.raises(ValueError):
            blend_models([(15.90, 0.92), (18.15, 0.73)], maes=[0.5, 0.0])

    def test_negative_mae_raises(self):
        with pytest.raises(ValueError):
            blend_models([(15.90, 0.92), (18.15, 0.73)], maes=[0.5, -1.0])
