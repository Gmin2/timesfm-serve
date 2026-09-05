"""tests for the benchmark's metrics and its weather handling.

the covariate tests exist because each of these silently produced a confident
wrong number once: nulls aggregating to zero, a month sized gap being
interpolated across, and a systematic offset between two archives being scored
as forecast error.
"""

import numpy as np
import pandas as pd
import pytest

from scripts import covariates as cv
from scripts.eval import coverage, crps, mape


def test_mape_zero_when_perfect():
    y = np.array([10.0, 20.0])
    assert mape(y, y) == 0.0


def test_crps_smaller_for_tighter_correct_quantiles():
    y = np.full(5, 100.0)
    tight = np.tile(np.linspace(99, 101, 9), (5, 1))
    wide = np.tile(np.linspace(50, 150, 9), (5, 1))
    assert crps(y, tight) < crps(y, wide)


def test_coverage_counts_actuals_inside_the_band():
    q = np.tile(np.linspace(90, 110, 9), (4, 1))  # p10 = 90, p90 = 110
    assert coverage(np.array([100.0, 100, 100, 100]), q) == 1.0
    assert coverage(np.array([100.0, 100, 100, 999]), q) == 0.75


def hourly_frame(days: int, hours_per_day: int = 24) -> pd.DataFrame:
    """days of hourly temperature, with only the first n hours populated"""
    index = pd.date_range("2024-01-01", periods=days * 24, freq="h")
    values = [20.0 if (i % 24) < hours_per_day else np.nan for i in range(days * 24)]
    return pd.DataFrame({"temperature_2m": values}, index=index)


def test_full_days_aggregate():
    daily = cv._to_daily(hourly_frame(3), temp_only=True)
    assert daily["temp_mean"].notna().all()
    assert daily["temp_mean"].iloc[0] == 20.0


def test_thin_days_become_missing_rather_than_wrong():
    # six of twenty four hours present. averaging those would look plausible
    # and summing them would be badly low, so the day has to be dropped.
    daily = cv._to_daily(hourly_frame(3, hours_per_day=6), temp_only=True)
    assert daily["temp_mean"].isna().all()


def test_rolling_bias_uses_only_the_past():
    index = pd.date_range("2024-01-01", periods=120, freq="D")
    actual = pd.DataFrame({"temp_mean": np.full(120, 20.0), "temp_max": np.full(120, 30.0)}, index=index)
    # forecast runs 2 degrees warm for the first half, then jumps to 10 warm
    warm = np.where(np.arange(120) < 60, 2.0, 10.0)
    leads = {1: pd.DataFrame({"temp_mean": 20.0 + warm, "temp_max": 30.0 + warm}, index=index)}

    bias = cv.rolling_bias(actual, leads, window=30)

    # on the day the jump happens the correction cannot know about it yet
    assert bias[1].loc[pd.Timestamp("2024-03-01"), "temp_mean"] == pytest.approx(2.0, abs=0.01)
    # and well after the jump it has caught up
    assert bias[1].loc[pd.Timestamp("2024-04-15"), "temp_mean"] == pytest.approx(10.0, abs=0.01)


def test_corrected_horizon_removes_a_constant_offset():
    index = pd.date_range("2024-01-01", periods=120, freq="D")
    actual = pd.DataFrame({"temp_mean": np.full(120, 20.0), "temp_max": np.full(120, 30.0)}, index=index)
    leads = {
        n: pd.DataFrame({"temp_mean": np.full(120, 24.0), "temp_max": np.full(120, 34.0)}, index=index)
        for n in range(1, cv.MAX_LEAD + 1)
    }
    bias = cv.rolling_bias(actual, leads, window=30)

    out = cv.horizon_covariates_corrected(leads, bias, pd.Timestamp("2024-03-01"), 7)
    assert len(out) == 7
    # the four degree offset is gone, leaving the truth
    assert out["temp_mean"].to_numpy() == pytest.approx(np.full(7, 20.0), abs=0.01)


def test_origin_is_skipped_when_a_forecast_day_is_missing():
    index = pd.date_range("2024-01-01", periods=120, freq="D")
    actual = pd.DataFrame({"temp_mean": np.full(120, 20.0), "temp_max": np.full(120, 30.0)}, index=index)
    leads = {
        n: pd.DataFrame({"temp_mean": np.full(120, 24.0), "temp_max": np.full(120, 34.0)}, index=index)
        for n in range(1, cv.MAX_LEAD + 1)
    }
    leads[3].loc[pd.Timestamp("2024-03-04"), "temp_mean"] = np.nan
    bias = cv.rolling_bias(actual, leads, window=30)

    # rather than invent a value for the missing day, the whole origin drops out
    assert cv.horizon_covariates_corrected(leads, bias, pd.Timestamp("2024-03-01"), 7).empty
