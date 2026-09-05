import copy
import json

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from scripts.weather_correction import FEATURES, STATIONS, apply_correction, fit_correction, prepare_case


def inputs():
    origin = pd.Timestamp("2025-02-01T06:00Z")
    index = pd.date_range(origin - pd.Timedelta(hours=167), periods=216, freq="h")
    observed = pd.Series(20 + 5 * np.sin(np.arange(216) * 2 * np.pi / 24), index=index)
    forecast = observed + 2
    return observed, forecast, origin


def training_rows():
    obs, forecast, origin = inputs()
    frames = []
    for station in STATIONS:
        frame, _ = prepare_case(obs, forecast, origin, station)
        frame["split"] = "train"
        frames.append(frame.reset_index())
    return pd.concat(frames, ignore_index=True)


def test_features_do_not_use_future_observations():
    obs, forecast, origin = inputs()
    first, history = prepare_case(obs, forecast, origin, STATIONS[0])
    obs.loc[obs.index > origin] = -200
    second, changed_history = prepare_case(obs, forecast, origin, STATIONS[0])
    pd.testing.assert_frame_equal(first[FEATURES], second[FEATURES])
    pd.testing.assert_frame_equal(history, changed_history)
    assert not first["observed_c"].equals(second["observed_c"])


def test_simple_baselines_use_the_same_causal_filled_history():
    obs, forecast, origin = inputs()
    obs.loc[origin] = np.nan
    obs.loc[origin - pd.Timedelta(hours=12)] = np.nan
    obs.loc[origin + pd.Timedelta(hours=2)] = np.nan
    frame, history = prepare_case(obs, forecast, origin, STATIONS[0])
    assert history["imputed"].sum() == 2
    assert frame["persistence_c"].eq(obs.loc[origin - pd.Timedelta(hours=1)]).all()
    np.testing.assert_array_equal(frame["yesterday_c"], np.tile(history["model_input_c"].iloc[-24:], 2))
    assert pd.isna(frame["observed_c"].iloc[1])
    assert not frame["scored"].iloc[1]
    assert frame["latest_source_age_hours"].eq(1).all()


@pytest.mark.parametrize("target", ["current", "future"])
def test_incomplete_forecast_is_rejected(target):
    obs, forecast, origin = inputs()
    forecast.loc[origin if target == "current" else origin + pd.Timedelta(hours=5)] = np.nan
    with pytest.raises(ValueError, match="Incomplete ECMWF"):
        prepare_case(obs, forecast, origin, STATIONS[0])


def test_portable_coefficients_match_sklearn_and_correct_known_bias():
    training = training_rows()
    cutoff = pd.Timestamp("2025-02-04T00:00Z")
    model = json.loads(json.dumps(fit_correction(training, cutoff)))
    pipeline = make_pipeline(StandardScaler(), Ridge(alpha=10))
    pipeline.fit(training[FEATURES], training["observed_c"] - training["ecmwf_ifs_c"])
    evaluation = training.copy()
    evaluation["origin"] = cutoff
    result = apply_correction(evaluation, model)
    np.testing.assert_allclose(result["ridge_corrected_c"] - result["ecmwf_ifs_c"], pipeline.predict(training[FEATURES]))
    np.testing.assert_allclose(result["bias_corrected_c"], training["observed_c"])


@pytest.mark.parametrize("problem", ["split", "cutoff", "overlap", "missing_station", "nonfinite"])
def test_fit_refuses_leaking_or_invalid_training(problem):
    training = training_rows()
    cutoff = pd.Timestamp("2025-02-04T00:00Z")
    if problem == "split":
        training.loc[0, "split"] = "holdout"
    elif problem == "cutoff":
        cutoff = pd.Timestamp("2025-02-03T06:00Z")
    elif problem == "overlap":
        training = pd.concat([training, training.iloc[:1]], ignore_index=True)
    elif problem == "missing_station":
        training = training.loc[training["station"] != STATIONS[0]]
    else:
        training.loc[0, "observed_c"] = np.inf
    with pytest.raises(ValueError):
        fit_correction(training, cutoff)


def test_fitted_model_cannot_predict_before_fit_cutoff():
    training = training_rows()
    model = fit_correction(training, pd.Timestamp("2025-02-04T00:00Z"))
    with pytest.raises(ValueError, match="before its fit cutoff"):
        apply_correction(training, model)


def test_predictions_do_not_use_evaluation_labels():
    training = training_rows()
    cutoff = pd.Timestamp("2025-02-04T00:00Z")
    model = fit_correction(training, cutoff)
    evaluation = training.copy()
    evaluation["origin"] = cutoff
    first = apply_correction(evaluation, model)
    evaluation["observed_c"] = -500
    second = apply_correction(evaluation, model)
    pd.testing.assert_frame_equal(first[["bias_corrected_c", "ridge_corrected_c"]], second[["bias_corrected_c", "ridge_corrected_c"]])


def test_corrupt_portable_model_fails():
    training = training_rows()
    cutoff = pd.Timestamp("2025-02-04T00:00Z")
    model = copy.deepcopy(fit_correction(training, cutoff))
    model["scale"] = [1]
    training["origin"] = cutoff
    with pytest.raises(ValueError, match="dimensions"):
        apply_correction(training, model)
