"""Train and evaluate the frozen, three-station ECMWF correction pilot."""

import argparse
import hashlib
import importlib.metadata
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.weather_case import CACHE, NOAA, ROOT, fetch, load_forecast, parse_observations, score_case, utc_hour
from scripts.weather_conditioned import IncompleteGuidance, TimesFMSession, load_guidance
from scripts.weather_correction import CONTEXT_HOURS, STATIONS, apply_correction, fit_correction, prepare_case
from scripts.weather_model import MODEL_ID, MODEL_REVISION, prepare_history, score_uncertainty

PROTOCOL = ROOT / "experiments" / "weather_correction_v1.json"
METHODS = ["persistence_c", "yesterday_c", "ecmwf_ifs_c", "bias_corrected_c", "ridge_corrected_c", "timesfm_c", "timesfm_nwp_c"]
CANDIDATES = ["bias_corrected_c", "ridge_corrected_c", "timesfm_c", "timesfm_nwp_c"]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def pipeline_hashes():
    names = ["weather_case.py", "weather_model.py", "weather_correction.py", "weather_conditioned.py", "weather_correction_backtest.py"]
    return {name: sha256(ROOT / "scripts" / name) for name in names}


def load_protocol(path):
    protocol = json.loads(path.read_text())
    expected = {
        "schema_version": 1, "target": "station_air_temperature_c", "observation_year": 2025,
        "horizon_hours": 48, "origin_step_hours": 48, "origin_utc_hour": 6, "minimum_run_age_hours": 6,
        "forecast_model": "ecmwf_ifs", "forecast_kind": "hindcast", "training_label_delay_hours": 1,
        "history_policy": {"context_hours": CONTEXT_HOURS, "max_gap_hours": 6, "max_imputed_fraction": 0.1},
        "observation_policy": {"report_type": "FM-15", "quality_codes": ["1", "5"], "exact_hours_only": True, "minimum_target_hours": 1, "fill_targets": False},
        "methods": METHODS,
        "selection": {"split": "development", "metric": "pooled_rmse_c", "candidates_in_tie_order": CANDIDATES, "refit_after_selection": False},
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"Unsupported correction protocol: {key}")
    if [station["id"] for station in protocol["stations"]] != STATIONS:
        raise ValueError("Station list differs from the frozen feature schema")
    if protocol["timesfm"]["model_id"] != MODEL_ID or protocol["timesfm"]["revision"] != MODEL_REVISION:
        raise ValueError("TimesFM checkpoint differs from the frozen candidate")
    if protocol["timesfm"]["point_estimate"] != "p50" or protocol["timesfm"]["fine_tuned"]:
        raise ValueError("Unsupported TimesFM inference policy")
    scoring = protocol["scoring"]
    if scoring["primary_baseline"] != "ecmwf_ifs_c" or scoring["primary_metric"] != "pooled_rmse_c" or not scoring["common_observation_mask"]:
        raise ValueError("Unsupported primary scoring policy")
    bootstrap = scoring["bootstrap"]
    if bootstrap["replicates"] < 100 or bootstrap["block_origins"] < 1:
        raise ValueError("Invalid bootstrap settings")
    schedule(protocol)
    return protocol, sha256(path)


def schedule(protocol):
    rows, dates = [], []
    seen = protocol["previously_scored_period_excluded"]
    seen_start, seen_end = utc_hour(seen["start"]), utc_hour(seen["end"])
    fit_cutoff = utc_hour(protocol["fit_cutoff"])
    for split in ["train", "development", "holdout"]:
        config = protocol["splits"][split]
        origins = pd.date_range(utc_hour(config["start"]), utc_hour(config["end"]), freq="48h")
        if len(origins) != config["origins_per_station"] or not len(origins):
            raise ValueError("Invalid scheduled origin count")
        for origin in origins:
            start, end = origin + pd.Timedelta(hours=1), origin + pd.Timedelta(hours=48)
            if origin.hour != 6 or (origin - pd.Timedelta(hours=192)).year != 2025 or end.year != 2025:
                raise ValueError("Origins must be 06 UTC with all inputs/targets in the source year")
            if start <= seen_end and end >= seen_start:
                raise ValueError("New experiment overlaps previously scored weather cases")
            if (split == "train" and end > fit_cutoff - pd.Timedelta(hours=1)) or (split != "train" and origin < fit_cutoff):
                raise ValueError("Training/evaluation violates fit cutoff")
            dates.append(origin)
            rows.extend({"split": split, "station": station, "origin": origin, "run": origin - pd.Timedelta(hours=6)} for station in STATIONS)
    dates = pd.Series(dates)
    if not dates.is_monotonic_increasing or not dates.is_unique or dates.diff().dropna().lt(pd.Timedelta(hours=48)).any():
        raise ValueError("Splits must be chronological with nonoverlapping target windows")
    return pd.DataFrame(rows)


def load_observations(protocol, cache_dir):
    observations, provenance = {}, {}
    for config in protocol["stations"]:
        station_id = config["id"]
        body, source = fetch(f"{NOAA}/2025/{station_id}.csv", cache_dir)
        if source["sha256"] != config["observation_sha256"]:
            raise ValueError(f"Observation snapshot changed for {station_id}")
        observed, station, audit = parse_observations(body, station_id)
        observations[station_id] = observed
        provenance[station_id] = {"station": station, "source": source, "audit": audit}
    return observations, provenance


def audit_origins(protocol, observations):
    rows = []
    for item in schedule(protocol).to_dict("records"):
        observed, origin = observations[item["station"]], item["origin"]
        target = observed.reindex(pd.date_range(origin + pd.Timedelta(hours=1), periods=48, freq="h"))
        reason, detail, imputed = "", "", None
        try:
            history = prepare_history(observed, origin, context_hours=CONTEXT_HOURS)
            imputed = int(history["imputed"].sum())
        except ValueError as error:
            reason, detail = "history_policy", str(error)
        if target.notna().sum() < 1:
            reason = ";".join(filter(None, [reason, "no_verification_observations"]))
        rows.append(item | {
            "status": "data_rejected" if reason else "eligible", "reason": reason, "detail": detail,
            "imputed_history_hours": imputed, "target_observed_hours": int(target.notna().sum()), "scored_hours": 0,
        })
    return pd.DataFrame(rows)


def artifact_hashes(output):
    return {str(path.relative_to(output)): sha256(path) for path in sorted(output.rglob("*")) if path.is_file() and path != output / "manifest.json"}


def verify_artifacts(output, hashes):
    if not hashes:
        raise ValueError("Missing artifact inventory")
    for name, digest in hashes.items():
        path = output / name
        if not path.resolve().is_relative_to(output.resolve()) or not path.is_file() or sha256(path) != digest:
            raise ValueError(f"Artifact verification failed: {path}")


def require_stage(root, stage, identity):
    path = root / stage / "manifest.json"
    if not path.exists():
        raise ValueError(f"Complete {stage} before proceeding")
    manifest = json.loads(path.read_text())
    if manifest.get("run_status") != "complete" or any(manifest.get(key) != value for key, value in identity.items()):
        raise ValueError(f"{stage} must complete with the exact same protocol, code and fitted model")
    verify_artifacts(path.parent, manifest.get("artifact_sha256"))
    return manifest


def read_frame(path):
    frame = pd.read_csv(path, dtype={"station": str})
    for column in ["origin", "valid_time"]:
        frame[column] = pd.to_datetime(frame[column], utc=True)
    if frame["scored"].dtype != bool:
        raise ValueError("Invalid saved scoring mask")
    return frame


def collect(protocol, ledger, observations, provenance, output, identity, args, model=None):
    frames, session = [], None
    ledger = ledger.copy().reset_index(drop=True)
    for i, row in ledger.iterrows():
        if row["status"] == "data_rejected":
            continue
        case_dir = output / "cases" / f"{row['station']}_{row['origin']:%Y%m%dT%H%MZ}"
        case_dir.mkdir(parents=True, exist_ok=True)
        case_manifest = case_dir / "manifest.json"
        success = "ready" if model is None else "scored"
        if case_manifest.exists():
            saved = json.loads(case_manifest.read_text())
            if any(saved.get(key) != value for key, value in identity.items()):
                raise ValueError("Case belongs to different code/protocol/model; use a new output directory")
            if saved.get("status") == success:
                verify_artifacts(case_dir, saved["artifact_sha256"])
                frame = read_frame(case_dir / "predictions.csv")
                frames.append(frame)
                ledger.loc[i, ["status", "scored_hours"]] = [success, int(frame["scored"].sum())]
                continue
        metadata = identity | {"station": row["station"], "origin": row["origin"].isoformat(), "run": row["run"].isoformat(), "status": "running"}
        write_json(case_manifest, metadata)
        for filename in ["predictions.csv", "model_input.csv", "guidance.csv"]:
            (case_dir / filename).unlink(missing_ok=True)
        stage = "forecast"
        print(f"[{row['split']}] {row['station']} {row['origin'].isoformat()}", flush=True)
        try:
            station = provenance[row["station"]]["station"]
            forecast, grid, source = load_forecast(station, row["run"], row["origin"], args.cache_dir)
            metadata |= {"forecast_source": source, "forecast_grid": grid}
            needed = pd.date_range(row["origin"], periods=49, freq="h")
            if not np.isfinite(forecast.reindex(needed).to_numpy()).all():
                raise IncompleteGuidance("Missing current-hour or future ECMWF temperature")
            stage = "preparation"
            frame, history = prepare_case(observations[row["station"]], forecast, row["origin"], row["station"])
            metadata["history_policy"] = history.attrs["history_policy"]
            if model is not None:
                stage = "forecast"
                guidance, sources = load_guidance(station, row["origin"], args.cache_dir)
                metadata["guidance_sources"] = sources
                if not np.array_equal(guidance["nwp_c"].iloc[-48:].to_numpy(), frame["ecmwf_ifs_c"].to_numpy()):
                    raise ValueError("TimesFM covariate must use exactly the benchmark's future guidance")
                stage = "inference"
                if session is None:
                    session = TimesFMSession(args.device, args.model_cache_dir, args.offline_model)
                predictions, model_inputs = session.predict(history, guidance)
                frame = apply_correction(frame, model).join(predictions)
                if not np.isfinite(frame[METHODS].to_numpy()).all():
                    raise ValueError("Every method must forecast the full horizon")
                metadata |= {"timesfm_model": session.metadata, "timesfm_inputs": model_inputs}
                guidance.to_csv(case_dir / "guidance.csv")
            stage = "artifact"
            frame["split"] = row["split"]
            history.to_csv(case_dir / "model_input.csv")
            frame.reset_index().to_csv(case_dir / "predictions.csv", index=False)
            metadata |= {"status": success, "artifact_sha256": artifact_hashes(case_dir)}
            write_json(case_manifest, metadata)
            frames.append(frame.reset_index())
            ledger.loc[i, ["status", "scored_hours"]] = [success, int(frame["scored"].sum())]
        except IncompleteGuidance as error:
            ledger.loc[i, ["status", "reason", "detail"]] = ["data_rejected", "incomplete_nwp", str(error)]
            write_json(case_manifest, metadata | {"status": "data_rejected", "detail": str(error)})
        except Exception as error:
            ledger.loc[i, ["status", "reason", "detail"]] = [f"{stage}_error", type(error).__name__, str(error)]
            write_json(case_manifest, metadata | {"status": f"{stage}_error", "detail": str(error)})
            print(f"  {stage} error: {error}", flush=True)
        ledger.to_csv(output / "origins.csv", index=False)
    ledger.to_csv(output / "origins.csv", index=False)
    return ledger, pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def summarize(ledger, predictions, protocol):
    accepted = ledger["status"].eq("scored")
    counts = {
        "planned_origins": len(ledger), "accepted_origins": int(accepted.sum()),
        "data_rejected_origins": int(ledger["status"].eq("data_rejected").sum()),
        "error_origins": int(ledger["status"].str.endswith("_error").sum()),
        "accepted_origin_fraction": float(accepted.mean()),
        "planned_hours": len(ledger) * 48,
        "observed_target_hours_all_origins": int(ledger["target_observed_hours"].sum()),
        "scored_hours": int(predictions["scored"].sum()) if not predictions.empty else 0,
        "accepted_origins_by_station": {station: int((accepted & ledger["station"].eq(station)).sum()) for station in STATIONS},
    }
    counts["scored_hour_fraction"] = counts["scored_hours"] / counts["planned_hours"]
    summary = {"counts": counts, "scores": {}, "uncertainty": {}, "rejection_reasons": ledger.loc[ledger["status"].eq("data_rejected"), "reason"].value_counts().to_dict()}
    tables = {}
    if predictions.empty:
        return summary, tables
    if not np.isfinite(predictions[METHODS].to_numpy()).all() or not predictions["scored"].equals(predictions["observed_c"].notna()):
        raise ValueError("All methods must use one unchanged verification mask")
    summary["scores"] = score_case(predictions, METHODS)
    for name, prefix in [("timesfm", "timesfm_"), ("timesfm_nwp", "timesfm_nwp_")]:
        selected = predictions[["observed_c", "scored", *[f"{prefix}p{q}_c" for q in range(10, 100, 10)]]].copy()
        selected = selected.rename(columns={column: column.replace(prefix, "timesfm_") for column in selected.columns})
        summary["uncertainty"][name] = score_uncertainty(selected)
    for label, keys in [("per_station", ["station"]), ("per_origin", ["station", "origin"]), ("per_lead", ["lead_hours"])]:
        rows = []
        groups = predictions.groupby(keys, sort=True)
        for values, group in groups:
            values = values if isinstance(values, tuple) else (values,)
            scores = score_case(group, METHODS) if group["scored"].any() else {method: {"n": 0, "mae_c": None, "rmse_c": None, "bias_c": None} for method in METHODS}
            rows.extend(dict(zip(keys, values, strict=True)) | {"method": method} | score for method, score in scores.items())
        tables[f"{label}.csv"] = pd.DataFrame(rows)
    bands = []
    for start, end in protocol["scoring"]["lead_bands"]:
        group = predictions.loc[predictions["lead_hours"].between(start, end)]
        scores = score_case(group, METHODS) if group["scored"].any() else {method: {"n": 0, "mae_c": None, "rmse_c": None, "bias_c": None} for method in METHODS}
        bands.extend({"lead_start": start, "lead_end": end, "method": method, **score} for method, score in scores.items())
    tables["lead_bands.csv"] = pd.DataFrame(bands)
    return summary, tables


def paired_bootstrap(predictions, scheduled_origins, candidate, config):
    """Paired date blocks retain station dependence and within-run dependence."""
    matched = predictions.loc[predictions["scored"]].copy()
    matched["candidate_sq"] = (matched[candidate] - matched["observed_c"]) ** 2
    matched["baseline_sq"] = (matched["ecmwf_ifs_c"] - matched["observed_c"]) ** 2
    matched["n"] = 1
    dates = pd.DatetimeIndex(pd.to_datetime(scheduled_origins, utc=True)).unique().sort_values()
    sums = matched.groupby("origin")[["candidate_sq", "baseline_sq", "n"]].sum().reindex(dates, fill_value=0).to_numpy()
    if sums[:, 2].sum() == 0:
        raise ValueError("No paired observations for bootstrap")
    rng = np.random.default_rng(config["seed"])
    width = config["block_origins"]
    replicates = config["replicates"]
    starts = rng.integers(0, len(dates), size=(replicates, int(np.ceil(len(dates) / width))))
    indices = ((starts[:, :, None] + np.arange(width)) % len(dates)).reshape(replicates, -1)[:, :len(dates)]
    totals = sums[indices].sum(axis=1)
    totals = totals[totals[:, 2] > 0]
    if not len(totals):
        raise ValueError("No nonempty bootstrap replicates")
    candidate_rmse = np.sqrt(totals[:, 0] / totals[:, 2])
    baseline_rmse = np.sqrt(totals[:, 1] / totals[:, 2])
    actual = sums.sum(axis=0)
    rmse, reference = np.sqrt(actual[:2] / actual[2])
    return {
        "candidate": candidate, "baseline": "ecmwf_ifs_c", "scheduled_origin_dates": len(dates),
        "paired_hours": int(actual[2]), "rmse_delta_c": float(rmse - reference),
        "rmse_skill_percent": float(100 * (1 - rmse / reference)) if reference > 0 else None,
        "rmse_delta_95ci_c": np.quantile(candidate_rmse - baseline_rmse, [0.025, 0.975]).tolist(),
        "replicates_with_data": len(totals), "bootstrap": config,
        "interpretation": "Negative delta favors the candidate; exploratory interval from one test month, not full-year validation.",
    }


def select_candidate(summary):
    if set(summary["scores"]) != set(METHODS):
        raise ValueError("Selection requires all candidate scores on the same development cases")
    return min(CANDIDATES, key=lambda method: summary["scores"][method]["rmse_c"])


def pilot_decision(summary, comparison, protocol):
    counts, rules = summary["counts"], protocol["scoring"]["pilot_pass"]
    checks = {
        "no_runtime_errors": counts["error_origins"] == 0,
        "origin_coverage": counts["accepted_origin_fraction"] >= rules["minimum_accepted_origin_fraction"],
        "hour_coverage": counts["scored_hour_fraction"] >= rules["minimum_scored_hour_fraction"],
        "station_coverage": all(value >= rules["minimum_accepted_origins_per_station"] for value in counts["accepted_origins_by_station"].values()),
        "rmse_improvement_interval": comparison["rmse_delta_95ci_c"][1] < 0,
    }
    return {"retrospective_pilot_pass": all(checks.values()), "checks": checks, "operational_ecmwf_superiority_established": False, "india_wide_or_indus_superiority_established": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=["audit", "train", "development", "holdout"])
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=CACHE)
    parser.add_argument("--device", choices=["mps", "cuda", "cpu"], default="mps")
    parser.add_argument("--model-cache-dir", type=Path)
    parser.add_argument("--offline-model", action="store_true")
    args = parser.parse_args()
    protocol, digest = load_protocol(args.protocol)
    identity = {
        "protocol_sha256": digest, "pipeline_sha256": pipeline_hashes(),
        "runtime_versions": {name: importlib.metadata.version(name) for name in ["numpy", "pandas", "scikit-learn", "torch", "timesfm"]},
    }
    root = args.output_dir or ROOT / "results" / "weather" / protocol["id"]
    root.mkdir(parents=True, exist_ok=True)
    snapshot = root / "protocol.json"
    if snapshot.exists() and sha256(snapshot) != digest:
        raise ValueError("Different protocol in output directory; never overwrite a frozen experiment")
    snapshot.write_bytes(args.protocol.read_bytes())
    model = None
    if args.stage in ["development", "holdout"]:
        require_stage(root, "train", identity)
        identity["correction_model_sha256"] = sha256(root / "train" / "model.json")
        model = json.loads((root / "train" / "model.json").read_text())
    development = require_stage(root, "development", identity) if args.stage == "holdout" else None
    if development is not None:
        verified_scores = score_case(read_frame(root / "development" / "predictions.csv"), METHODS)
        if development.get("selected_candidate") != select_candidate({"scores": verified_scores}):
            raise ValueError("Saved selection differs from verified development predictions")
    output = root / args.stage
    output.mkdir(parents=True, exist_ok=True)
    existing = output / "manifest.json"
    if existing.exists() and args.stage != "audit":
        saved = json.loads(existing.read_text())
        if any(saved.get(key) != value for key, value in identity.items()):
            raise ValueError("Output has different code/protocol/model; use a new output directory")
        if saved.get("run_status") == "complete":
            verify_artifacts(output, saved["artifact_sha256"])
            print(f"Already complete and verified: {output}")
            return
    manifest = identity | {"stage": args.stage, "run_status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "limits": protocol["limits"]}
    write_json(existing, manifest)
    observations, provenance = load_observations(protocol, args.cache_dir)
    audit = audit_origins(protocol, observations)
    manifest["observations"] = provenance
    if args.stage == "audit":
        audit.to_csv(output / "origins.csv", index=False)
        write_json(existing, manifest | {"run_status": "complete", "computes_forecast_scores": False, "artifact_sha256": artifact_hashes(output)})
        print(audit.groupby(["split", "station", "status"]).size().to_string())
        return
    selected = audit.loc[audit["split"].eq(args.stage)]
    selected.to_csv(output / "origins.csv", index=False)
    ledger, predictions = collect(protocol, selected, observations, provenance, output, identity, args, model)
    complete = ledger["status"].isin(["ready", "scored", "data_rejected"]).all() and not predictions.empty
    if not predictions.empty:
        predictions.to_csv(output / "predictions.csv", index=False)
    if complete and args.stage == "train":
        fitted = fit_correction(predictions, utc_hour(protocol["fit_cutoff"]), protocol["ridge_alpha"])
        fitted |= {"training_predictions_sha256": sha256(output / "predictions.csv"), "scikit_learn_version": importlib.metadata.version("scikit-learn")}
        write_json(output / "model.json", fitted)
        print(f"Fitted on {fitted['training_matched_hours']} matched hours; station counts: {fitted['training_hours_by_station']}")
    elif args.stage != "train":
        summary, tables = summarize(ledger, predictions, protocol)
        for filename, table in tables.items():
            table.to_csv(output / filename, index=False)
        manifest["results"] = summary
        if complete:
            if args.stage == "development":
                manifest["selected_candidate"] = select_candidate(summary)
            else:
                candidate = development["selected_candidate"]
                comparison = paired_bootstrap(predictions, ledger["origin"], candidate, protocol["scoring"]["bootstrap"])
                manifest |= {"selected_candidate": candidate, "primary_comparison": comparison, "decision": pilot_decision(summary, comparison, protocol)}
        if summary["scores"]:
            print(pd.DataFrame(summary["scores"]).T.round(3).to_string())
    manifest |= {"run_status": "complete" if complete else "incomplete", "finished_at": datetime.now(timezone.utc).isoformat(), "artifact_sha256": artifact_hashes(output)}
    write_json(existing, manifest)
    print(f"{args.stage}: {ledger['status'].value_counts().to_dict()}; {manifest['run_status']}; {output}")
    if not complete:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
