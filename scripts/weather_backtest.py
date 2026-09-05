"""Run the frozen single-station temperature backtest in verified slices."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.weather_case import CACHE, METHODS, NOAA, ROOT, STATION, build_case, fetch, load_forecast, parse_observations, score_case, utc_hour
from scripts.weather_model import HISTORY_POLICY, MODEL_ID, MODEL_REVISION, QUANTILE_COLUMNS, predict_timesfm, prepare_history, score_uncertainty

PROTOCOL = ROOT / "experiments" / "weather_backtest_v1.json"
PREDICTION_COLUMNS = ["split", "origin", "valid_time", "lead_hours", "run_lead_hours", "observed_c", *METHODS, "scored", "timesfm_c", *QUANTILE_COLUMNS]


def load_protocol(path):
    body = path.read_bytes()
    protocol = json.loads(body)
    expected = {
        "schema_version": 1,
        "station": STATION,
        "observation_year": 2025,
        "target": "station_air_temperature_c",
        "horizon_hours": 48,
        "origin_step_hours": 48,
        "minimum_run_age_hours": 6,
        "forecast_model": "ecmwf_ifs",
        "forecast_kind": "hindcast",
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "history_policy": HISTORY_POLICY,
        "methods": [*METHODS, "timesfm_c"],
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"Protocol {key} differs from this runner's supported configuration")
    if protocol["eligibility"] != {
        "complete_previous_24_hours": True,
        "minimum_observed_target_hours": 1,
        "complete_forecast_horizon": True,
        "observation_report_type": "FM-15",
        "accepted_quality_codes": ["1", "5"],
    }:
        raise ValueError("Unsupported eligibility rules")
    return protocol, hashlib.sha256(body).hexdigest()


def schedule(protocol):
    rows = []
    seen_start = utc_hour(protocol["previously_seen_window"]["start"])
    seen_end = utc_hour(protocol["previously_seen_window"]["end"])
    for split in ["development", "holdout"]:
        config = protocol["splits"][split]
        origins = pd.date_range(utc_hour(config["start"]), utc_hour(config["end"]), freq="48h")
        if len(origins) != config["origins"] or len(origins) == 0:
            raise ValueError("Scheduled origin count does not match the protocol")
        for origin in origins:
            end = origin + pd.Timedelta(hours=48)
            start = origin + pd.Timedelta(hours=1)
            if origin.hour != 6 or (origin - pd.Timedelta(hours=671)).year != protocol["observation_year"] or end.year != protocol["observation_year"]:
                raise ValueError("Origins must be at 06 UTC with context/targets inside the observation year")
            if start <= seen_end and end >= seen_start:
                raise ValueError("Evaluation overlaps the previously inspected demonstration")
            rows.append({"split": split, "origin": origin, "run": origin - pd.Timedelta(hours=6)})
    result = pd.DataFrame(rows)
    if not result["origin"].is_unique or not result["origin"].is_monotonic_increasing:
        raise ValueError("Splits must be chronological with unique origins")
    if result["origin"].diff().dropna().lt(pd.Timedelta(hours=48)).any():
        raise ValueError("Evaluation target windows must not overlap")
    return result


def audit_origins(observed, protocol):
    rows = []
    for item in schedule(protocol).to_dict("records"):
        origin = item["origin"]
        history_index = pd.date_range(end=origin, periods=672, freq="h")
        target_index = pd.date_range(start=origin + pd.Timedelta(hours=1), periods=48, freq="h")
        history = observed.reindex(history_index)
        target = observed.reindex(target_index)
        previous_day_missing = int(history.iloc[-24:].isna().sum())
        reasons = []
        detail = ""
        max_age = None
        try:
            prepared = prepare_history(observed, origin)
            max_age = int(prepared["source_age_hours"].max())
        except ValueError as error:
            reasons.append("history_policy")
            detail = str(error)
        if previous_day_missing:
            reasons.append("incomplete_previous_day")
        if target.notna().sum() < protocol["eligibility"]["minimum_observed_target_hours"]:
            reasons.append("no_verification_observations")
        rows.append(item | {
            "status": "data_rejected" if reasons else "eligible",
            "reason": ";".join(reasons),
            "detail": detail,
            "history_observed_hours": int(history.notna().sum()),
            "history_missing_hours": int(history.isna().sum()),
            "previous_day_missing_hours": previous_day_missing,
            "target_observed_hours": int(target.notna().sum()),
            "max_source_age_hours": max_age,
        })
    return pd.DataFrame(rows)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def pipeline_hashes():
    return {
        name: hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
        for name in ["weather_backtest.py", "weather_case.py", "weather_model.py"]
    }


def require_development(path, digest, hashes):
    if not path.exists():
        raise ValueError("Verify the development split before opening the holdout")
    development = json.loads(path.read_text())
    if (
        development.get("run_status") != "complete"
        or development.get("protocol_sha256") != digest
        or development.get("pipeline_sha256") != hashes
        or development.get("results", {}).get("counts", {}).get("accepted_origins", 0) == 0
    ):
        raise ValueError("Development must score at least one case and complete with this exact protocol and pipeline before the holdout")


def score_rows(frame, methods):
    if not frame["scored"].any():
        return [{"method": method, "n": 0, "mae_c": None, "rmse_c": None, "bias_c": None} for method in methods]
    return [{"method": method, **score} for method, score in score_case(frame, methods).items()]


def summarize(ledger, predictions, protocol):
    accepted = int(ledger["status"].eq("scored").sum())
    errors = int(ledger["status"].str.endswith("_error").sum())
    scored_hours = int(predictions["scored"].sum()) if not predictions.empty else 0
    counts = {
        "planned_origins": len(ledger),
        "accepted_origins": accepted,
        "data_rejected_origins": int(ledger["status"].eq("data_rejected").sum()),
        "error_origins": errors,
        "accepted_origin_fraction": accepted / len(ledger),
        "planned_forecast_hours": len(ledger) * 48,
        "observed_target_hours_all_origins": int(ledger["target_observed_hours"].sum()),
        "scored_forecast_hours": scored_hours,
        "scored_fraction_of_planned_hours": scored_hours / (len(ledger) * 48),
        "scored_fraction_of_accepted_hours": scored_hours / (accepted * 48) if accepted else None,
    }
    summary = {"counts": counts, "rejection_reason_combinations": ledger.loc[ledger["status"].eq("data_rejected"), "reason"].value_counts().to_dict()}
    per_origin, per_lead, bands = [], [], []
    if predictions.empty:
        summary |= {"scores": {}, "uncertainty": {}, "paired_origin_comparisons": {}}
    else:
        methods = protocol["methods"]
        summary["scores"] = score_case(predictions, methods)
        summary["uncertainty"] = score_uncertainty(predictions)
        for origin, group in predictions.groupby("origin", sort=True):
            per_origin.extend({"origin": origin, **row} for row in score_rows(group, methods))
        for lead in range(1, 49):
            group = predictions.loc[predictions["lead_hours"].eq(lead)]
            per_lead.extend({"lead_hours": lead, **row} for row in score_rows(group, methods))
        for start, end in protocol["scoring"]["lead_bands"]:
            group = predictions.loc[predictions["lead_hours"].between(start, end)]
            bands.extend({"lead_start": start, "lead_end": end, **row} for row in score_rows(group, methods))
        paired = pd.DataFrame(per_origin).pivot(index="origin", columns="method", values="mae_c")
        comparisons = {}
        for baseline in METHODS:
            delta = paired["timesfm_c"] - paired[baseline]
            ties = np.isclose(delta, 0, atol=1e-8, rtol=0)
            comparisons[baseline] = {
                "n_origins": len(delta),
                "timesfm_wins": int(((delta < 0) & ~ties).sum()),
                "ties": int(ties.sum()),
                "timesfm_losses": int(((delta > 0) & ~ties).sum()),
                "mean_origin_mae_delta_c": float(delta.mean()),
                "delta_definition": "TimesFM MAE minus baseline MAE on the same origin; negative is better for TimesFM.",
            }
        summary["paired_origin_comparisons"] = comparisons
    tables = {
        "per_origin.csv": pd.DataFrame(per_origin, columns=["origin", "method", "n", "mae_c", "rmse_c", "bias_c"]),
        "per_lead.csv": pd.DataFrame(per_lead, columns=["lead_hours", "method", "n", "mae_c", "rmse_c", "bias_c"]),
        "lead_bands.csv": pd.DataFrame(bands, columns=["lead_start", "lead_end", "method", "n", "mae_c", "rmse_c", "bias_c"]),
    }
    return summary, tables


def evaluate_split(observed, station, audit, protocol, output, device, cache_dir, model_cache_dir, offline_model):
    ledger = audit.copy().reset_index(drop=True)
    ledger["scored_hours"] = 0
    ledger.to_csv(output / "origins.csv", index=False)
    pd.DataFrame(columns=PREDICTION_COLUMNS).to_csv(output / "predictions.csv", index=False)
    predictions = []
    for i, row in ledger.iterrows():
        if row["status"] == "data_rejected":
            continue
        origin, run = row["origin"], row["run"]
        case_dir = output / "cases" / origin.strftime("%Y%m%dT%H%MZ")
        case_dir.mkdir(parents=True, exist_ok=True)
        metadata = {"origin": origin.isoformat(), "run": run.isoformat(), "forecast_kind": protocol["forecast_kind"]}
        write_json(case_dir / "manifest.json", metadata | {"status": "running"})
        # A failed replay must not leave a previous successful forecast looking current.
        for name in ["model_input.csv", "forecast.csv"]:
            (case_dir / name).unlink(missing_ok=True)
        stage = "forecast"
        print(f"[{row['split']}] {origin.isoformat()}: fetching frozen run", flush=True)
        try:
            forecast, grid, source = load_forecast(station, run, origin, cache_dir)
            metadata |= {"forecast_source": source, "forecast_grid": grid}
            target_index = pd.date_range(start=origin + pd.Timedelta(hours=1), periods=48, freq="h")
            if forecast.reindex(target_index).isna().any():
                ledger.loc[i, ["status", "reason", "detail"]] = ["data_rejected", "incomplete_forecast_horizon", "Selected archived run contains missing forecast hours"]
            else:
                case = build_case(observed, forecast, origin)
                case.insert(1, "run_lead_hours", [int((time - run) / pd.Timedelta(hours=1)) for time in case.index])
                prepared = prepare_history(observed, origin)
                stage = "inference"
                model_forecast, model_metadata = predict_timesfm(
                    prepared, case.index, device=device, cache_dir=model_cache_dir, local_files_only=offline_model,
                )
                case = case.join(model_forecast)
                metadata |= {"timesfm": model_metadata, "scores": score_case(case, protocol["methods"]), "uncertainty": score_uncertainty(case)}
                stage = "artifact"
                prepared.to_csv(case_dir / "model_input.csv")
                case.to_csv(case_dir / "forecast.csv")
                case = case.reset_index()
                case.insert(0, "origin", origin.isoformat())
                case.insert(0, "split", row["split"])
                predictions.append(case)
                ledger.loc[i, "status"] = "scored"
                ledger.loc[i, "scored_hours"] = int(case["scored"].sum())
                print(f"[{row['split']}] {origin.isoformat()}: scored {int(case['scored'].sum())}/48 hours", flush=True)
        except Exception as error:
            ledger.loc[i, ["status", "reason", "detail"]] = [f"{stage}_error", type(error).__name__, str(error)]
            print(f"[{row['split']}] {origin.isoformat()}: {stage} error: {error}", flush=True)
        metadata |= {"status": ledger.loc[i, "status"], "reason": ledger.loc[i, "reason"], "detail": ledger.loc[i, "detail"]}
        write_json(case_dir / "manifest.json", metadata)
        ledger.to_csv(output / "origins.csv", index=False)
    combined = pd.concat(predictions, ignore_index=True) if predictions else pd.DataFrame(columns=PREDICTION_COLUMNS)
    return ledger, combined


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--cache-dir", type=Path, default=CACHE)
    parser.add_argument("--output-dir", type=Path, help="Study root; audit and each split get separate subdirectories")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--split", choices=["development", "holdout"])
    parser.add_argument("--device", choices=["cpu", "mps", "cuda"], default="cpu")
    parser.add_argument("--model-cache-dir", type=Path)
    parser.add_argument("--offline-model", action="store_true")
    args = parser.parse_args()
    if args.audit_only == bool(args.split):
        parser.error("Choose either --audit-only or --split development/holdout")
    protocol, digest = load_protocol(args.protocol)
    root = args.output_dir or ROOT / "results" / "weather" / protocol["id"]
    root.mkdir(parents=True, exist_ok=True)
    snapshot = root / "protocol.json"
    if snapshot.exists() and hashlib.sha256(snapshot.read_bytes()).hexdigest() != digest:
        raise ValueError("Saved protocol differs; do not overwrite an experiment with changed rules")
    snapshot.write_bytes(args.protocol.read_bytes())
    hashes = pipeline_hashes()
    if args.split == "holdout":
        require_development(root / "development" / "manifest.json", digest, hashes)
    body, source = fetch(f"{NOAA}/{protocol['observation_year']}/{STATION}.csv", args.cache_dir)
    if source["sha256"] != protocol["observation_sha256"]:
        raise ValueError("Observation snapshot changed; this frozen protocol requires the original cached bytes")
    observed, station, observation_audit = parse_observations(body)
    audit = audit_origins(observed, protocol)
    output = root / ("audit" if args.audit_only else args.split)
    output.mkdir(parents=True, exist_ok=True)
    (output / "protocol.json").write_bytes(args.protocol.read_bytes())
    audit.to_csv(output / "origins.csv", index=False)
    manifest = {
        "stage": "eligibility_audit_only" if args.audit_only else "frozen_backtest",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": digest,
        "pipeline_sha256": hashes,
        "observation_source": source,
        "station": station,
        "observation_audit": observation_audit,
        "planned_origins": len(audit),
        "eligible_origins": int(audit["status"].eq("eligible").sum()),
        "command_computes_forecast_scores": not args.audit_only,
    }
    if args.audit_only:
        write_json(output / "manifest.json", manifest)
        print(audit.groupby(["split", "status"]).size().to_string())
        print("Rejection reasons:", audit.loc[audit["status"].eq("data_rejected"), "reason"].value_counts().to_dict())
        print(f"Frozen protocol SHA-256: {digest}")
        print(f"Audit artifacts: {output}")
        return
    manifest |= {"run_status": "running", "split": args.split, "limitations": protocol["limits"]}
    selected = audit.loc[audit["split"].eq(args.split)].copy()
    manifest["planned_origins"] = len(selected)
    manifest["eligible_origins"] = int(selected["status"].eq("eligible").sum())
    write_json(output / "manifest.json", manifest)
    ledger, predictions = evaluate_split(
        observed, station, selected, protocol, output, args.device, args.cache_dir, args.model_cache_dir, args.offline_model,
    )
    summary, tables = summarize(ledger, predictions, protocol)
    for filename, table in tables.items():
        table.to_csv(output / filename, index=False)
    predictions.to_csv(output / "predictions.csv", index=False)
    complete = ledger["status"].isin(["scored", "data_rejected"]).all()
    manifest |= {"run_status": "complete" if complete else "incomplete", "finished_at": datetime.now(timezone.utc).isoformat(), "results": summary}
    write_json(output / "manifest.json", manifest)
    print(json.dumps(summary["counts"], indent=2))
    if summary["scores"]:
        print(pd.DataFrame(summary["scores"]).T.round(3).to_string())
    print(f"Results: {output}; status: {manifest['run_status']}")
    if not complete:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
