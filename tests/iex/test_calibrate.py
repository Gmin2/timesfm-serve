import numpy as np
import pandas as pd
import pytest

from iex.backtest import BLOCKS, QUANTILE_LEVELS
from iex.calibrate import COLUMNS, calibrate, cap_probability, coverage, offsets, pinball


def forecasts(days=200, shift=0.0, seed=5, first="2024-06-18"):
    """Quantiles deliberately offset from the truth, so calibration has work to do."""
    generator = np.random.default_rng(seed)
    rows = []
    for day in pd.date_range(first, periods=days, freq="D"):
        actual = generator.normal(3000, 500, BLOCKS)
        frame = pd.DataFrame({
            "delivery_date": day, "block": range(1, BLOCKS + 1),
            "model": "stub", "actual": actual, "at_cap": actual >= 9999.0,
        })
        for level, column in zip(QUANTILE_LEVELS, COLUMNS, strict=True):
            # Quantiles of the same normal the actuals came from, plus a shift
            # that calibration has to remove.
            frame[column] = 3000 + shift + 500 * _probit(level)
        frame["forecast"] = frame[COLUMNS[len(COLUMNS) // 2]]
        rows.append(frame)
    return pd.concat(rows, ignore_index=True)


def _probit(level):
    from scipy.stats import norm

    return float(norm.ppf(level))


def test_coverage_reports_each_level_and_the_band():
    results = forecasts(shift=0.0)
    table, inside = coverage(results)
    assert list(table["level"]) == list(QUANTILE_LEVELS)
    # Well-specified quantiles should land close to their nominal level.
    assert (table["gap"].abs() < 0.05).all()
    assert inside == pytest.approx(0.8, abs=0.05)


def test_calibration_fixes_a_shifted_band():
    results = forecasts(shift=800.0)  # every quantile far too high
    _, inside_before = coverage(results)
    adjusted = calibrate(results, window=90)
    scored = adjusted[adjusted["calibrated"]]
    _, inside_after = coverage(scored)
    assert abs(inside_after - 0.8) < abs(inside_before - 0.8)
    table, _ = coverage(scored)
    assert (table["gap"].abs() < 0.06).all()


def test_early_days_pass_through_uncalibrated():
    adjusted = calibrate(forecasts(days=60, shift=500.0), window=90)
    first_days = sorted(adjusted["delivery_date"].unique())[:30]
    assert not adjusted[adjusted["delivery_date"].isin(first_days)]["calibrated"].any()
    assert adjusted[~adjusted["delivery_date"].isin(first_days)]["calibrated"].all()


def test_calibration_never_reads_the_day_it_is_correcting():
    results = forecasts(days=150, shift=400.0)
    adjusted = calibrate(results, window=90)
    target = sorted(adjusted["delivery_date"].unique())[-1]

    poisoned = results.copy()
    later = poisoned["delivery_date"] >= target
    poisoned.loc[later, "actual"] = poisoned.loc[later, "actual"] * 5 + 9000
    repeated = calibrate(poisoned, window=90)

    before = adjusted[adjusted["delivery_date"] == target][COLUMNS].to_numpy()
    after = repeated[repeated["delivery_date"] == target][COLUMNS].to_numpy()
    assert np.allclose(before, after)


def test_calibrated_quantiles_stay_in_order():
    adjusted = calibrate(forecasts(shift=600.0), window=90)
    values = adjusted[adjusted["calibrated"]][COLUMNS].to_numpy()
    assert (np.diff(values, axis=1) >= -1e-9).all()


def test_offsets_move_a_biased_quantile_the_right_way():
    past = forecasts(days=100, shift=1000.0)
    corrections = offsets(past)
    # Quantiles sit above the truth, so every correction should pull them down.
    assert all(value < 0 for value in corrections.values())


def test_pinball_rewards_the_better_band():
    good = forecasts(shift=0.0)
    bad = forecasts(shift=1500.0)
    assert pinball(good) < pinball(bad)


def test_cap_probability_beats_guessing_when_caps_are_predictable():
    results = forecasts(days=120)
    # Make the top quantile reach the cap exactly when the outcome does.
    hit = results["block"] > 80
    results.loc[hit, "actual"] = 10_000.0
    results["at_cap"] = results["actual"] >= 9999.0
    results.loc[hit, COLUMNS[-1]] = 10_000.0
    scores = cap_probability(results)
    assert scores["brier"] < scores["climatology_brier"]


def test_calibrate_refuses_results_without_quantiles():
    results = forecasts().drop(columns=COLUMNS)
    with pytest.raises(ValueError, match="no quantiles"):
        calibrate(results)
