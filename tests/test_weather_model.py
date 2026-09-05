from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.weather_model import QUANTILE_COLUMNS, QUANTILE_LEVELS, forecast_frame, predict_timesfm, prepare_history, score_uncertainty


def history_series(hours=100):
    index = pd.date_range("2025-06-01", periods=hours, freq="h", tz="UTC")
    return pd.Series(np.arange(hours, dtype=float), index=index)


def test_forward_fill_records_each_source_without_changing_observations():
    observed = history_series()
    observed.iloc[10:16] = np.nan
    before = observed.copy()
    prepared = prepare_history(observed, observed.index[-1], context_hours=100)
    pd.testing.assert_series_equal(observed, before)
    assert prepared["imputed"].sum() == 6
    assert prepared["observed_c"].iloc[10:16].isna().all()
    assert prepared["model_input_c"].iloc[10:16].eq(9).all()
    assert prepared["source_time"].iloc[10:16].eq(observed.index[9]).all()
    assert prepared["source_age_hours"].iloc[10:16].tolist() == [1, 2, 3, 4, 5, 6]
    assert prepared.loc[~prepared["imputed"], "source_age_hours"].eq(0).all()


def test_future_values_cannot_change_model_input_or_fill_trailing_gap():
    observed = history_series(150)
    origin = observed.index[99]
    observed.iloc[97:100] = np.nan
    first = prepare_history(observed, origin, context_hours=100)
    observed.iloc[100:] = -9999.0
    second = prepare_history(observed, origin, context_hours=100)
    pd.testing.assert_frame_equal(first, second)
    assert first["model_input_c"].iloc[-3:].eq(96).all()
    assert first.index.max() == origin
    assert (first["source_time"] <= first.index).all()


def test_missing_timestamps_become_auditable_gaps():
    observed = history_series()
    origin = observed.index[-1]
    prepared = prepare_history(observed.drop(observed.index[10]), origin, context_hours=100)
    assert len(prepared) == 100
    assert prepared["imputed"].iloc[10]
    assert prepared["model_input_c"].iloc[10] == 9


@pytest.mark.parametrize("start,stop", [(0, 1), (10, 17)])
def test_leading_or_long_gaps_are_rejected(start, stop):
    observed = history_series()
    observed.iloc[start:stop] = np.nan
    with pytest.raises(ValueError, match="leading gap or a gap longer"):
        prepare_history(observed, observed.index[-1], context_hours=100)


def test_windows_above_ten_percent_missing_are_rejected():
    observed = history_series()
    observed.iloc[1:23:2] = np.nan
    with pytest.raises(ValueError, match="exceeds 10%"):
        prepare_history(observed, observed.index[-1], context_hours=100)


def test_exact_missing_fraction_limit_is_accepted():
    observed = history_series()
    observed.iloc[1:21:2] = np.nan
    prepared = prepare_history(observed, observed.index[-1], context_hours=100)
    assert prepared["imputed"].sum() == 10


def test_infinity_is_not_treated_as_a_valid_observation():
    observed = history_series()
    observed.iloc[10] = np.inf
    with pytest.raises(ValueError, match="non-finite"):
        prepare_history(observed, observed.index[-1], context_hours=100)


def test_duplicate_timestamps_are_rejected():
    observed = history_series()
    with pytest.raises(ValueError, match="unique"):
        prepare_history(pd.concat([observed, observed.iloc[:1]]), observed.index[-1], context_hours=100)


def test_custom_policy_and_indian_timezone_are_recorded_correctly():
    observed = history_series()
    prepared = prepare_history(observed, observed.index[-1].tz_convert("Asia/Kolkata"), context_hours=100, max_gap_hours=3)
    assert str(prepared.index.tz) == "UTC"
    assert prepared.attrs["history_policy"]["context_hours"] == 100
    assert prepared.attrs["history_policy"]["max_gap_hours"] == 3


def output_example():
    index = pd.date_range("2025-07-01", periods=3, freq="h", tz="UTC")
    quantiles = np.tile(np.arange(-9.0, 0.0), (3, 1))
    return SimpleNamespace(forecast=quantiles[:, 4].copy(), quantiles=quantiles), index


def test_valid_forecast_preserves_median_quantiles_and_negative_temperature():
    output, index = output_example()
    frame = forecast_frame(output, index, QUANTILE_LEVELS)
    assert frame.index.equals(index)
    assert frame["timesfm_c"].eq(-5.0).all()
    pd.testing.assert_series_equal(frame["timesfm_c"], frame["timesfm_p50_c"], check_names=False)
    assert frame["timesfm_p10_c"].eq(-9.0).all()
    assert frame["timesfm_p90_c"].eq(-1.0).all()


@pytest.mark.parametrize("problem", ["point_shape", "quantile_shape", "nan", "infinity", "crossed", "median", "levels"])
def test_bad_model_outputs_fail_before_scoring(problem):
    output, index = output_example()
    levels = QUANTILE_LEVELS
    if problem == "point_shape":
        output.forecast = output.forecast[:2]
    elif problem == "quantile_shape":
        output.quantiles = output.quantiles[:, :8]
    elif problem == "nan":
        output.forecast[0] = np.nan
    elif problem == "infinity":
        output.quantiles[0, 0] = -np.inf
    elif problem == "crossed":
        output.quantiles[0, 0] = 100.0
    elif problem == "median":
        output.forecast[0] += 1.0
    elif problem == "levels":
        levels = [q / 10 for q in range(9)]
    with pytest.raises(ValueError):
        forecast_frame(output, index, levels)


def test_model_refuses_nonfinite_or_leaking_inputs_before_loading():
    observed = history_series()
    history = prepare_history(observed, observed.index[-1], context_hours=100)
    future = pd.date_range(start=history.index[-1] + pd.Timedelta(hours=1), periods=48, freq="h")
    with pytest.raises(ValueError, match="immediately before"):
        predict_timesfm(history, future - pd.Timedelta(hours=1))
    with pytest.raises(ValueError, match="consecutive"):
        predict_timesfm(history, future.delete(1))
    history.loc[history.index[5], "model_input_c"] = np.nan
    with pytest.raises(ValueError, match="finite context"):
        predict_timesfm(history, future)


def test_uncertainty_scores_use_the_same_mask_and_expected_pinball_loss():
    case = pd.DataFrame(np.tile(np.arange(-4.0, 5.0), (2, 1)), columns=QUANTILE_COLUMNS)
    case["observed_c"] = [0.0, np.nan]
    case["scored"] = [True, False]
    score = score_uncertainty(case)
    assert score == pytest.approx({
        "n": 1,
        "mean_quantile_pinball_c": 4 / 9,
        "p10_p90_coverage": 1.0,
        "p10_p90_nominal_coverage": 0.8,
        "mean_p10_p90_width_c": 8.0,
    })
    case["scored"] = False
    with pytest.raises(ValueError, match="No matched"):
        score_uncertainty(case)


def test_interval_coverage_includes_boundaries():
    case = pd.DataFrame(np.tile(np.arange(-4.0, 5.0), (3, 1)), columns=QUANTILE_COLUMNS)
    case["observed_c"] = [-4.0, 4.0, 5.0]
    case["scored"] = True
    assert score_uncertainty(case)["p10_p90_coverage"] == pytest.approx(2 / 3)


@pytest.mark.parametrize("problem", ["nan", "crossed"])
def test_invalid_quantiles_cannot_produce_an_uncertainty_score(problem):
    case = pd.DataFrame(np.tile(np.arange(-4.0, 5.0), (1, 1)), columns=QUANTILE_COLUMNS)
    case["observed_c"] = 0.0
    case["scored"] = True
    case["timesfm_p10_c"] = np.nan if problem == "nan" else 100.0
    with pytest.raises(ValueError):
        score_uncertainty(case)
