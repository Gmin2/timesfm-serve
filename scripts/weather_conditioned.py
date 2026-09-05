"""TimesFM with an explicitly issued, finite ECMWF guidance covariate."""

import hashlib
import importlib.metadata
import time

import numpy as np
import pandas as pd

from scripts.weather_case import CACHE, load_forecast
from scripts.weather_correction import CONTEXT_HOURS
from scripts.weather_model import MODEL_ID, MODEL_REVISION, forecast_frame


class IncompleteGuidance(ValueError):
    pass


def load_guidance(station, origin, cache_dir=CACHE):
    """Use earlier fixed runs for past context, never future realized weather."""
    pieces, sources = [], []
    # Four preceding 48-hour segments cover the seven-day model context.
    for offset in [192, 144, 96, 48, 0]:
        segment_origin = origin - pd.Timedelta(hours=offset)
        run = segment_origin - pd.Timedelta(hours=6)
        forecast, grid, source = load_forecast(station, run, segment_origin, cache_dir)
        index = pd.date_range(segment_origin + pd.Timedelta(hours=1), periods=48, freq="h", name="valid_time")
        pieces.append(pd.DataFrame({
            "nwp_c": forecast.reindex(index),
            "segment_origin": segment_origin,
            "run": run,
            "run_lead_hours": range(7, 55),
        }, index=index))
        sources.append({"segment_origin": segment_origin.isoformat(), "run": run.isoformat(), "source": source, "grid": grid})
    wanted = pd.date_range(origin - pd.Timedelta(hours=CONTEXT_HOURS - 1), periods=CONTEXT_HOURS + 48, freq="h", name="valid_time")
    guidance = pd.concat(pieces).reindex(wanted)
    if not np.isfinite(guidance["nwp_c"].to_numpy()).all():
        raise IncompleteGuidance("Selected ECMWF runs do not cover the complete covariate context and horizon")
    return guidance, sources


def validate_inputs(history, guidance):
    if len(history) != CONTEXT_HOURS or history.attrs.get("history_policy", {}).get("context_hours") != CONTEXT_HOURS:
        raise ValueError("Conditioned TimesFM requires the explicit seven-day input policy")
    expected = pd.date_range(history.index[0], periods=CONTEXT_HOURS + 48, freq="h")
    if not guidance.index.equals(expected) or not history.index.equals(expected[:CONTEXT_HOURS]):
        raise ValueError("Guidance and observation context must align exactly")
    context = history["model_input_c"].to_numpy(dtype=np.float32)
    covariate = guidance["nwp_c"].to_numpy(dtype=np.float32)
    if not np.isfinite(context).all() or not np.isfinite(covariate).all():
        raise ValueError("Finite explicit inputs are required; no wrapper interpolation")
    origin = history.index[-1]
    segments = pd.to_datetime(guidance["segment_origin"], utc=True)
    runs = pd.to_datetime(guidance["run"], utc=True)
    lead = (guidance.index.to_series() - runs).dt.total_seconds() / 3600
    if (
        segments.isna().any() or runs.isna().any()
        or (runs != segments - pd.Timedelta(hours=6)).any()
        or (segments >= guidance.index).any() or (segments > origin).any()
        or not lead.between(7, 54).all()
        or not np.array_equal(lead.to_numpy(), guidance["run_lead_hours"].to_numpy())
        or not segments.iloc[CONTEXT_HOURS:].eq(origin).all()
    ):
        raise ValueError("Guidance uses invalid run timing or future information")
    return context, covariate


class TimesFMSession:
    """Keep the pinned model loaded for the two candidate forecasts per case."""

    def __init__(self, device, cache_dir=None, local_files_only=False):
        import timesfm
        import torch
        from huggingface_hub import snapshot_download

        self.torch, self.device = torch, device
        if device == "mps" and not torch.backends.mps.is_available():
            raise ValueError("MPS was requested but is unavailable")
        if device == "cuda" and not torch.cuda.is_available():
            raise ValueError("CUDA was requested but is unavailable")
        snapshot = snapshot_download(
            MODEL_ID, revision=MODEL_REVISION, allow_patterns=["config.json", "model.safetensors"],
            cache_dir=cache_dir, local_files_only=local_files_only,
        )
        from pathlib import Path

        hashes = {}
        for name in ["config.json", "model.safetensors"]:
            with (Path(snapshot) / name).open("rb") as stream:
                hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        started = time.perf_counter()
        self.model = timesfm.TimesFM3Forecaster.from_pretrained(snapshot, device=device, local_files_only=True)
        self.synchronize()
        self.options = {
            "horizon": 48, "return_quantiles": True, "sort_quantiles": True,
            "make_positive": False, "use_symmetric_averaging": False, "use_znorm": False, "padding_mode": "none",
        }
        self.metadata = {
            "model_id": MODEL_ID, "revision": MODEL_REVISION, "device": device,
            "checkpoint_sha256": hashes, "point_estimate": "p50", "fine_tuned": False,
            "packages": {name: importlib.metadata.version(name) for name in ["timesfm", "torch", "numpy", "pandas"]},
            "predict_options": self.options, "load_seconds": time.perf_counter() - started,
        }

    def synchronize(self):
        if self.device == "mps":
            self.torch.mps.synchronize()
        elif self.device == "cuda":
            self.torch.cuda.synchronize()

    def predict(self, history, guidance):
        context, covariate = validate_inputs(history, guidance)
        index = guidance.index[CONTEXT_HOURS:]
        outputs, timings = [], {}
        for name, inputs in [("history", {}), ("nwp", {"past_future_covariates": covariate[None, :]})]:
            started = time.perf_counter()
            output = self.model.predict(context, **self.options, **inputs)
            self.synchronize()
            timings[name] = time.perf_counter() - started
            frame = forecast_frame(output, index, self.model.config.quantiles)
            if name == "nwp":
                frame = frame.rename(columns=lambda column: column.replace("timesfm_", "timesfm_nwp_"))
            outputs.append(frame)
        return outputs[0].join(outputs[1]), {
            "context_float32_sha256": hashlib.sha256(context.astype("<f4").tobytes()).hexdigest(),
            "covariate_float32_sha256": hashlib.sha256(covariate.astype("<f4").tobytes()).hexdigest(),
            "context_hours": len(context), "future_hours": 48, "covariate_shape": [1, len(covariate)],
            "imputed_observation_hours": int(history["imputed"].sum()),
            "predict_seconds": timings,
        }
