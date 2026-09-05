from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts import weather_conditioned as conditioned
from scripts.weather_model import QUANTILE_LEVELS, prepare_history


def prepared(monkeypatch):
    origin = pd.Timestamp("2025-02-04T06:00Z")
    index = pd.date_range(origin - pd.Timedelta(hours=167), periods=168, freq="h")
    history = prepare_history(pd.Series(np.arange(168, dtype=float), index=index), origin, context_hours=168)
    calls = []

    def forecast(station, run, segment_origin, cache):
        calls.append((run, segment_origin))
        times = pd.date_range(run, periods=55, freq="h")
        return pd.Series(np.arange(55, dtype=float), index=times), {}, {"run": str(run)}

    monkeypatch.setattr(conditioned, "load_forecast", forecast)
    guidance, sources = conditioned.load_guidance({}, origin)
    return history, guidance, sources, calls


def test_guidance_uses_past_fixed_runs_and_current_future_run(monkeypatch):
    history, guidance, sources, calls = prepared(monkeypatch)
    context, covariate = conditioned.validate_inputs(history, guidance)
    assert context.shape == (168,)
    assert covariate.shape == (216,)
    assert len(sources) == len(calls) == 5
    assert all(run == origin - pd.Timedelta(hours=6) for run, origin in calls)
    assert guidance["segment_origin"].iloc[-48:].eq(history.index[-1]).all()
    assert guidance["run"].iloc[:168].max() < history.index[-1] - pd.Timedelta(hours=6)
    np.testing.assert_array_equal(covariate[-48:], np.arange(7, 55))


@pytest.mark.parametrize("problem", ["future_run", "future_segment", "gap", "nan", "false_lead"])
def test_invalid_guidance_never_reaches_model(monkeypatch, problem):
    history, guidance, _, _ = prepared(monkeypatch)
    if problem == "future_run":
        guidance.loc[guidance.index[0], "run"] = history.index[-1] + pd.Timedelta(hours=6)
    elif problem == "future_segment":
        guidance.loc[guidance.index[-1], "segment_origin"] = history.index[-1] + pd.Timedelta(hours=1)
    elif problem == "gap":
        guidance = guidance.drop(guidance.index[0])
    elif problem == "nan":
        guidance.loc[guidance.index[5], "nwp_c"] = np.nan
    else:
        guidance.loc[guidance.index[0], "run_lead_hours"] = 0
    with pytest.raises(ValueError):
        conditioned.validate_inputs(history, guidance)


def test_missing_forecast_is_not_interpolated(monkeypatch):
    origin = pd.Timestamp("2025-02-04T06:00Z")

    def forecast(station, run, segment_origin, cache):
        times = pd.date_range(run, periods=55, freq="h")
        values = np.ones(55)
        values[35] = np.nan
        return pd.Series(values, index=times), {}, {}

    monkeypatch.setattr(conditioned, "load_forecast", forecast)
    with pytest.raises(conditioned.IncompleteGuidance):
        conditioned.load_guidance({}, origin)


def test_two_predictions_pass_correct_covariate_shape(monkeypatch):
    history, guidance, _, _ = prepared(monkeypatch)
    calls = []

    def predict(context, **options):
        calls.append((context.copy(), options))
        quantiles = np.tile(np.arange(-4.0, 5.0), (48, 1))
        return SimpleNamespace(forecast=quantiles[:, 4], quantiles=quantiles)

    session = conditioned.TimesFMSession.__new__(conditioned.TimesFMSession)
    session.model = SimpleNamespace(predict=predict, config=SimpleNamespace(quantiles=QUANTILE_LEVELS))
    session.options = {"horizon": 48}
    session.synchronize = lambda: None
    result, metadata = session.predict(history, guidance)
    assert result.shape == (48, 20)
    assert "past_future_covariates" not in calls[0][1]
    assert calls[1][1]["past_future_covariates"].shape == (1, 216)
    np.testing.assert_array_equal(calls[0][0], calls[1][0])
    assert metadata["covariate_shape"] == [1, 216]
    assert result.index[0] == history.index[-1] + pd.Timedelta(hours=1)
