"""GPU-only adapter over the frozen weather experiment's input/inference code."""

import hashlib
import io
from datetime import datetime, timezone

from timesfm_serve import weather_catalog


class InvalidReplay(ValueError):
    pass


def read_inputs(case_id, expected_manifest):
    import numpy as np
    import pandas as pd

    from scripts.weather_conditioned import validate_inputs
    from scripts.weather_model import prepare_history

    directory, manifest, digest = weather_catalog.case_manifest(case_id)
    if digest != expected_manifest:
        raise InvalidReplay("manifest_changed")
    for name in ("weather_model.py", "weather_conditioned.py", "weather_correction.py", "weather_case.py"):
        source = (weather_catalog.ROOT / "scripts" / name).read_bytes()
        if hashlib.sha256(source).hexdigest() != manifest["pipeline_sha256"][name]:
            raise InvalidReplay("frozen_inference_code_changed")
    origin = pd.Timestamp(manifest["origin"])
    if case_id != f"{manifest['station']}_{origin.strftime('%Y%m%dT%H%MZ')}":
        raise InvalidReplay("case_identity_mismatch")
    if origin.tzinfo is None or origin + pd.Timedelta(hours=48) >= pd.Timestamp.now(tz="UTC"):
        raise InvalidReplay("historical_inputs_required")
    frames = {}
    for name in ("model_input.csv", "guidance.csv"):
        raw = (directory / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest["artifact_sha256"][name]:
            raise InvalidReplay("input_hash_mismatch")
        frames[name] = pd.read_csv(io.BytesIO(raw), index_col="valid_time", parse_dates=["valid_time"])
    saved = frames["model_input.csv"]
    history = prepare_history(saved["observed_c"], origin, context_hours=168)
    if history.attrs["history_policy"] != manifest["history_policy"]:
        raise InvalidReplay("history_policy_mismatch")
    if not history.index.equals(saved.index) or not np.array_equal(history["model_input_c"], saved["model_input_c"]):
        raise InvalidReplay("noncausal_history")
    guidance = frames["guidance.csv"]
    context, covariate = validate_inputs(history, guidance)
    for name, values in (("context", context), ("covariate", covariate)):
        if hashlib.sha256(values.astype("<f4").tobytes()).hexdigest() != manifest["timesfm_inputs"][f"{name}_float32_sha256"]:
            raise InvalidReplay("model_input_hash_mismatch")
    return history, guidance, manifest


class WeatherEngine:
    def __init__(self, device="cuda", cache_dir=None):
        if device not in ("cuda", "mps"):
            raise ValueError("Weather inference requires cuda (AWS) or mps (local); CPU fallback is disabled")
        from scripts.weather_conditioned import TimesFMSession

        self.session = TimesFMSession(device, cache_dir=cache_dir, local_files_only=True)

    def warmup(self):
        cases = weather_catalog.catalog()
        if not cases:
            raise InvalidReplay("No verified replay inputs are mounted")
        case = cases[0]
        history, guidance, manifest = read_inputs(case["case_id"], case["manifest_sha256"])
        self._check_model(manifest)
        self.session.predict(history, guidance)

    def _check_model(self, manifest):
        for key in ("model_id", "revision", "checkpoint_sha256", "predict_options"):
            if self.session.metadata[key] != manifest["timesfm_model"][key]:
                raise InvalidReplay("checkpoint_or_options_mismatch")

    def predict(self, job):
        if job.get("kind") == "live":
            return self.predict_live(job)
        history, guidance, manifest = read_inputs(job["case_id"], job["manifest_sha256"])
        self._check_model(manifest)
        predictions, metadata = self.session.predict(history, guidance)
        points = []
        for valid_time, row in predictions.iterrows():
            points.append({
                "valid_time": valid_time.isoformat(),
                "temperature_2m": float(row["timesfm_nwp_c"]),
                "ecmwf_ifs_temperature_2m": float(guidance.loc[valid_time, "nwp_c"]),
                "quantiles": [float(row[f"timesfm_nwp_p{q}_c"]) for q in range(10, 100, 10)],
            })
        return {
            "job_id": str(job["id"]), "case_id": job["case_id"],
            "station_id": manifest["station"], "mode": "historical_replay",
            "operational_forecast": False,
            "forecast_origin": manifest["origin"], "guidance_run": manifest["run"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "horizon_hours": 48, "interval_hours": 1, "unit": "celsius",
            "model": "timesfm_nwp", "point_estimate": "p50",
            "quantile_levels": [q / 10 for q in range(1, 10)],
            "quantiles_calibrated": False,
            "model_provenance": self.session.metadata,
            "input_provenance": {
                "manifest_sha256": job["manifest_sha256"],
                "source_snapshot_sha256": manifest["source_snapshot_sha256"],
                "guidance_sources": manifest["guidance_sources"],
                "historical_publication_time_verified": False,
                "history_policy": manifest["history_policy"],
                **metadata,
            },
            "points": points,
        }

    def predict_live(self, job):
        from datetime import timedelta

        from timesfm_serve import weather_live_store
        from timesfm_serve.weather_ingest import reconstruct
        from timesfm_serve.weather_live_policy import POLICY, SERVE_MAX_AGE, check_issue_window, parse_utc, utc_now

        snapshot = weather_live_store.load_snapshot(job)
        origin = parse_utc(snapshot["forecast_origin"])
        check_issue_window(origin, utc_now())
        history, guidance = reconstruct(snapshot)
        if self.session.metadata["revision"] != snapshot["model_revision"] or self.session.metadata["model_id"] != snapshot["model_id"]:
            raise ValueError("Live model revision mismatch")
        predictions, metadata = self.session.predict(history, guidance)
        generated_at = utc_now()
        check_issue_window(origin, generated_at)
        return {
            "job_id": str(job["id"]), "input_snapshot_id": str(job["input_snapshot_id"]),
            "case_id": job["case_id"], "station_id": snapshot["station_id"],
            "mode": "experimental_live", "production_validated": False, "policy": POLICY,
            "forecast_origin": origin.isoformat(), "guidance_run": (origin - timedelta(hours=6)).isoformat(),
            "inputs_captured_at": snapshot["captured_at"],
            "latest_observation_at": snapshot["observation_audit"]["latest_observation_at"],
            "generated_at": generated_at.isoformat(), "fresh_until": (origin + SERVE_MAX_AGE).isoformat(),
            "horizon_hours": 48, "interval_hours": 1, "unit": "celsius", "model": "timesfm_nwp",
            "point_estimate": "p50", "quantile_levels": [q / 10 for q in range(1, 10)], "quantiles_calibrated": False,
            "model_provenance": self.session.metadata,
            "input_provenance": {
                "snapshot_sha256": job["manifest_sha256"], "history_policy": snapshot["history_policy"],
                "observation_audit": snapshot["observation_audit"],
                "sources": [{k: v for k, v in source.items() if k != "body"} for source in snapshot["sources"]],
                "inference_source_sha256": snapshot["inference_source_sha256"], **metadata,
            },
            "points": [{
                "valid_time": t.isoformat(), "temperature_2m": float(row["timesfm_nwp_c"]),
                "ecmwf_ifs_temperature_2m": float(guidance.loc[t, "nwp_c"]),
                "quantiles": [float(row[f"timesfm_nwp_p{q}_c"]) for q in range(10, 100, 10)],
            } for t, row in predictions.iterrows()],
        }
