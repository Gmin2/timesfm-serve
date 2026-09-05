"""Causal inputs and train-only residual correction for station temperature."""

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from scripts.weather_case import utc_hour
from scripts.weather_model import prepare_history

STATIONS = ["42410099999", "43128599999", "43279099999"]
CONTEXT_HOURS = 168
FEATURES = [
    "ecmwf_ifs_c", "lead_days", "hour_sin", "hour_cos", "year_sin", "year_cos",
    "recent_error_c", "recent_error_decay_c", "previous_day_difference_c",
    "history_mean_difference_c", "history_std_c", "latest_source_age_hours",
    *[f"station_{station}" for station in STATIONS],
]
CORRECTION_METHODS = ["ecmwf_ifs_c", "bias_corrected_c", "ridge_corrected_c"]


def prepare_case(observed, forecast, origin, station_id):
    """Build features using only observations at or before this origin."""
    origin = utc_hour(origin)
    if station_id not in STATIONS:
        raise ValueError("Station is not part of this correction experiment")
    history = prepare_history(observed, origin, context_hours=CONTEXT_HOURS)
    index = pd.date_range(origin + pd.Timedelta(hours=1), periods=48, freq="h", name="valid_time")
    nwp = forecast.reindex(index)
    current_nwp = forecast.reindex([origin]).iloc[0]
    if not np.isfinite(nwp.to_numpy()).all() or not np.isfinite(current_nwp):
        raise ValueError("Incomplete ECMWF current-hour or forecast horizon")
    values = history["model_input_c"]
    lead = np.arange(1, 49)
    local = index.tz_convert("Asia/Kolkata")
    hour = local.hour + local.minute / 60
    previous_day = np.tile(values.iloc[-24:].to_numpy(), 2)
    recent_error = float(values.iloc[-1] - current_nwp)
    frame = pd.DataFrame({
        "station": station_id,
        "origin": origin,
        "lead_hours": lead,
        "observed_c": observed.reindex(index),
        "persistence_c": float(values.iloc[-1]),
        "yesterday_c": previous_day,
        "ecmwf_ifs_c": nwp,
        "lead_days": lead / 24,
        "hour_sin": np.sin(2 * np.pi * hour / 24),
        "hour_cos": np.cos(2 * np.pi * hour / 24),
        "year_sin": np.sin(2 * np.pi * (local.dayofyear - 1) / 365.25),
        "year_cos": np.cos(2 * np.pi * (local.dayofyear - 1) / 365.25),
        "recent_error_c": recent_error,
        "recent_error_decay_c": recent_error * np.exp(-lead / 12),
        "previous_day_difference_c": previous_day - nwp,
        "history_mean_difference_c": float(values.mean()) - nwp,
        "history_std_c": float(values.std(ddof=0)),
        "latest_source_age_hours": int(history["source_age_hours"].iloc[-1]),
    }, index=index)
    for station in STATIONS:
        frame[f"station_{station}"] = float(station == station_id)
    frame["scored"] = frame["observed_c"].notna()
    if not np.isfinite(frame.loc[frame["scored"], "observed_c"]).all() or not np.isfinite(frame[FEATURES].to_numpy()).all():
        raise ValueError("Correction features must be finite")
    return frame, history


def fit_correction(training, cutoff, alpha=10.0):
    """Fit on a named training split; never refit on development/test labels."""
    cutoff = utc_hour(cutoff)
    if not np.isfinite(alpha) or alpha <= 0:
        raise ValueError("Ridge alpha must be positive and finite")
    if training.empty or not training["split"].eq("train").all():
        raise ValueError("Fit requires nonempty training-only rows")
    valid_times = pd.to_datetime(training["valid_time"], utc=True)
    origins = pd.to_datetime(training["origin"], utc=True)
    if valid_times.isna().any() or origins.isna().any() or (valid_times <= origins).any() or (valid_times > origins + pd.Timedelta(hours=48)).any():
        raise ValueError("Invalid training forecast times")
    if (valid_times > cutoff - pd.Timedelta(hours=1)).any() or (origins >= cutoff).any():
        raise ValueError("Training labels must precede the fit cutoff by at least one hour")
    if training.duplicated(["station", "valid_time"]).any():
        raise ValueError("Training target windows must not overlap at a station")
    matched = training.loc[training["scored"]].copy()
    if set(matched["station"]) != set(STATIONS):
        raise ValueError("Every planned station needs observed training targets")
    counts = matched.groupby("station").size()
    if (counts < 48).any():
        raise ValueError("Each station needs at least 48 matched training hours")
    residual = matched["observed_c"] - matched["ecmwf_ifs_c"]
    if not np.isfinite(matched[FEATURES].to_numpy()).all() or not np.isfinite(residual).all():
        raise ValueError("Training features and observed residuals must be finite")
    pipeline = make_pipeline(StandardScaler(), Ridge(alpha=alpha))
    pipeline.fit(matched[FEATURES], residual)
    scaler, ridge = pipeline.steps[0][1], pipeline.steps[1][1]
    return {
        "schema_version": 1,
        "kind": "standardized_ridge_residual",
        "features": FEATURES,
        "alpha": alpha,
        "fit_cutoff": cutoff.isoformat(),
        "training_max_valid_time": valid_times.max().isoformat(),
        "training_matched_hours": len(matched),
        "training_hours_by_station": {str(key): int(value) for key, value in counts.items()},
        "station_bias_c": residual.groupby(matched["station"]).mean().to_dict(),
        "mean": scaler.mean_.tolist(),
        "scale": scaler.scale_.tolist(),
        "coef": ridge.coef_.tolist(),
        "intercept": float(ridge.intercept_),
    }


def apply_correction(frame, model):
    if model["features"] != FEATURES or model["kind"] != "standardized_ridge_residual" or model["schema_version"] != 1:
        raise ValueError("Unsupported correction artifact")
    origins = pd.to_datetime(frame["origin"], utc=True)
    if origins.isna().any() or (origins < pd.Timestamp(model["fit_cutoff"])).any():
        raise ValueError("Cannot evaluate a fitted model before its fit cutoff")
    features = frame[FEATURES].to_numpy(dtype=float)
    scale = np.asarray(model["scale"])
    mean, coef = np.asarray(model["mean"]), np.asarray(model["coef"])
    if any(value.shape != (len(FEATURES),) or not np.isfinite(value).all() for value in [scale, mean, coef]):
        raise ValueError("Invalid correction artifact dimensions or parameters")
    if not np.isfinite(features).all() or (scale <= 0).any() or not np.isfinite(model["intercept"]):
        raise ValueError("Invalid correction features or scaling")
    correction = ((features - mean) / scale) @ coef + model["intercept"]
    bias = frame["station"].map(model["station_bias_c"])
    if bias.isna().any() or not np.isfinite(correction).all():
        raise ValueError("Missing station bias or non-finite correction")
    result = frame.copy()
    result["bias_corrected_c"] = result["ecmwf_ifs_c"] + bias
    result["ridge_corrected_c"] = result["ecmwf_ifs_c"] + correction
    return result
