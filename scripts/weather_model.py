"""Explicit history preparation and TimesFM inference for hourly temperature."""

import hashlib
import importlib.metadata
import platform
import time
from pathlib import Path

import numpy as np
import pandas as pd

MODEL_ID = "google/timesfm-3.0-pytorch"
MODEL_REVISION = "43046b85ec22d584a13f8098c2ed39c889e129c2"
QUANTILE_LEVELS = [q / 10 for q in range(1, 10)]
QUANTILE_COLUMNS = [f"timesfm_p{q * 10}_c" for q in range(1, 10)]
HISTORY_POLICY = {
    "name": "causal_forward_fill_v1",
    "context_hours": 672,
    "max_gap_hours": 6,
    "max_imputed_fraction": 0.1,
    "mask_usage": "audit_only_not_a_model_covariate",
}


def prepare_history(observed, origin, context_hours=672, max_gap_hours=6, max_imputed_fraction=0.1):
    """Prepare a bounded, causal context; never modify observations or targets."""
    if context_hours < 1 or max_gap_hours < 1 or not 0 <= max_imputed_fraction < 1:
        raise ValueError("Invalid history policy limits")
    if origin.tzinfo is None:
        raise ValueError("Origin must be a timezone-aware exact hour")
    origin = origin.tz_convert("UTC")
    if origin != origin.floor("h"):
        raise ValueError("Origin must be a timezone-aware exact hour")
    if not isinstance(observed.index, pd.DatetimeIndex) or observed.index.tz is None or not observed.index.is_unique:
        raise ValueError("Observations need unique timezone-aware timestamps")
    index = pd.date_range(end=origin.tz_convert("UTC"), periods=context_hours, freq="h", name="valid_time")
    history = observed.reindex(index).rename("observed_c")
    if not np.isfinite(history.dropna().to_numpy()).all():
        raise ValueError("Observed history contains non-finite values")
    missing = history.isna()
    if missing.mean() > max_imputed_fraction:
        raise ValueError(f"Missing history exceeds {max_imputed_fraction:.0%} of the context")
    filled = history.ffill(limit=max_gap_hours)
    if filled.isna().any():
        raise ValueError(f"History has a leading gap or a gap longer than {max_gap_hours} hours")

    source = pd.Series(index, index=index).where(~missing).ffill(limit=max_gap_hours)
    age = (pd.Series(index, index=index) - source).dt.total_seconds() / 3600
    prepared = pd.DataFrame({
        "observed_c": history,
        "model_input_c": filled,
        "imputed": missing,
        "source_time": source,
        "source_age_hours": age.astype(int),
    }, index=index)
    prepared.attrs["history_policy"] = HISTORY_POLICY | {
        "context_hours": context_hours,
        "max_gap_hours": max_gap_hours,
        "max_imputed_fraction": max_imputed_fraction,
    }
    return prepared


def forecast_frame(output, index, quantile_levels):
    """Validate the observed TimesFM return contract before scoring any values."""
    point = np.asarray(output.forecast, dtype=float)
    quantiles = np.asarray(output.quantiles, dtype=float)
    if list(quantile_levels) != QUANTILE_LEVELS:
        raise ValueError("Expected TimesFM quantiles p10 through p90")
    if point.shape != (len(index),) or quantiles.shape != (len(index), 9):
        raise ValueError("Unexpected TimesFM output shape")
    if not np.isfinite(point).all() or not np.isfinite(quantiles).all():
        raise ValueError("TimesFM returned non-finite predictions")
    if (np.diff(quantiles, axis=1) < 0).any():
        raise ValueError("TimesFM quantiles are not ordered")
    if not np.allclose(point, quantiles[:, 4], atol=1e-6, rtol=0):
        raise ValueError("TimesFM point forecast must equal p50")
    result = pd.DataFrame(quantiles, columns=QUANTILE_COLUMNS, index=index)
    result.insert(0, "timesfm_c", point)
    return result


def predict_timesfm(history, index, device="cpu", cache_dir=None, local_files_only=False):
    context = history["model_input_c"].to_numpy(dtype=np.float32)
    if not len(context) or not np.isfinite(context).all():
        raise ValueError("TimesFM requires an explicitly prepared finite context")
    if not len(index) or history.index[-1] + pd.Timedelta(hours=1) != index[0]:
        raise ValueError("Model history must end immediately before the forecast horizon")
    if not index.equals(pd.date_range(start=index[0], periods=len(index), freq="h")):
        raise ValueError("Forecast horizon must have consecutive hourly timestamps")
    policy = history.attrs.get("history_policy")
    if policy is None or policy["context_hours"] != len(context):
        raise ValueError("Prepared history must include matching policy metadata")

    import timesfm
    import torch
    from huggingface_hub import snapshot_download

    snapshot = Path(snapshot_download(
        MODEL_ID, revision=MODEL_REVISION, allow_patterns=["config.json", "model.safetensors"],
        cache_dir=cache_dir, local_files_only=local_files_only,
    ))
    hashes = {}
    for filename in ["config.json", "model.safetensors"]:
        with (snapshot / filename).open("rb") as stream:
            hashes[filename] = hashlib.file_digest(stream, "sha256").hexdigest()

    def synchronize():
        if device == "mps":
            torch.mps.synchronize()
        elif device == "cuda":
            torch.cuda.synchronize()

    started = time.perf_counter()
    model = timesfm.TimesFM3Forecaster.from_pretrained(str(snapshot), device=device, local_files_only=True)
    synchronize()
    load_seconds = time.perf_counter() - started
    options = {
        "horizon": len(index),
        "return_quantiles": True,
        "sort_quantiles": True,
        "make_positive": False,
        "use_symmetric_averaging": False,
        "use_znorm": False,
        "padding_mode": "none",
    }
    started = time.perf_counter()
    output = model.predict(context, **options)
    synchronize()
    predict_seconds = time.perf_counter() - started
    result = forecast_frame(output, index, model.config.quantiles)
    metadata = {
        "model_id": MODEL_ID,
        "revision": MODEL_REVISION,
        "checkpoint_sha256": hashes,
        "device": device,
        "platform": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        "packages": {name: importlib.metadata.version(name) for name in ["timesfm", "torch", "numpy", "pandas", "huggingface-hub"]},
        "point_estimate": "p50",
        "quantile_levels": QUANTILE_LEVELS,
        "predict_options": options,
        "history_policy": policy.copy(),
        "context_start": history.index[0].isoformat(),
        "context_end": history.index[-1].isoformat(),
        "context_hours": len(history),
        "context_float32_sha256": hashlib.sha256(context.astype("<f4").tobytes()).hexdigest(),
        "imputed_hours": int(history["imputed"].sum()),
        "imputed_fraction": float(history["imputed"].mean()),
        "max_source_age_hours": int(history["source_age_hours"].max()),
        "covariates": None,
        "fine_tuned": False,
        "model_load_seconds": load_seconds,
        "predict_seconds": predict_seconds,
        "timing_scope": "One inference call including wrapper processing; excludes loading and data I/O. Not a service latency benchmark.",
    }
    return result, metadata


def score_uncertainty(case):
    matched = case.loc[case["scored"]]
    if matched.empty:
        raise ValueError("No matched observations to score uncertainty")
    quantiles = matched[QUANTILE_COLUMNS].to_numpy()
    actual = matched["observed_c"].to_numpy()
    if not np.isfinite(quantiles).all() or not np.isfinite(actual).all():
        raise ValueError("Uncertainty scores require finite values")
    if (np.diff(quantiles, axis=1) < 0).any():
        raise ValueError("Uncertainty scores require ordered quantiles")
    error = actual[:, None] - quantiles
    levels = np.asarray(QUANTILE_LEVELS)
    pinball = np.maximum(levels * error, (levels - 1) * error)
    return {
        "n": len(actual),
        "mean_quantile_pinball_c": float(pinball.mean()),
        "p10_p90_coverage": float(((actual >= quantiles[:, 0]) & (actual <= quantiles[:, -1])).mean()),
        "p10_p90_nominal_coverage": 0.8,
        "mean_p10_p90_width_c": float((quantiles[:, -1] - quantiles[:, 0]).mean()),
    }
