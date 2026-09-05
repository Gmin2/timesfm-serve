"""Frozen cross-year, multi-season weather comparison using GHCNh observations."""

import argparse
import importlib.metadata
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from scripts import weather_correction_backtest as pilot
from scripts.weather_case import CACHE, ROOT, score_case, utc_hour
from scripts.weather_correction import STATIONS, fit_correction
from scripts.weather_model import MODEL_ID, MODEL_REVISION, prepare_history
from scripts.weather_seasonal_data import POLICY, load_years
from scripts.weather_seasonal_data import STATIONS as SOURCE_STATIONS

PROTOCOL = ROOT / "experiments/weather_seasonal_v1.json"
EPOCH_BOUNDARY = pd.Timestamp("2026-05-12T06:00Z")


def forecast_epoch(run):
    return "ifs_49r1_hindcast" if run < EPOCH_BOUNDARY else "ifs_50r1_archive_availability_unverified"


def calendar_season(month):
    return {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM", 6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}[month]


def schedule(protocol):
    rows, all_dates = [], []
    cutoff = utc_hour(protocol["fit_cutoff"])
    seen = protocol["previously_scored_evaluation_period_excluded"]
    for split in ["train", "development", "holdout"]:
        config = protocol["splits"][split]
        dates = pd.date_range(utc_hour(config["start"]), utc_hour(config["end"]), freq="192h")
        if len(dates) != config["origins_per_station"] or not len(dates) or dates[-1] != utc_hour(config["end"]):
            raise ValueError("Seasonal schedule count/end does not match the frozen specification")
        for origin in dates:
            start, end = origin + pd.Timedelta(hours=1), origin + pd.Timedelta(hours=48)
            if origin.hour != 6 or origin - pd.Timedelta(hours=198) < pd.Timestamp("2024-03-14T00:00Z"):
                raise ValueError("Origin or its oldest guidance run is outside the supported archive")
            if start <= utc_hour(seen["end"]) and end >= utc_hour(seen["start"]):
                raise ValueError("Seasonal windows overlap previously scored evaluation dates")
            if (split == "train" and end >= cutoff) or (split != "train" and origin < cutoff):
                raise ValueError("Seasonal split violates the training cutoff")
            if end > pd.Timestamp("2026-09-01T00:00Z"):
                raise ValueError("Targets extend beyond the frozen observation period")
            run = origin - pd.Timedelta(hours=6)
            rows.extend({
                "split": split, "station": station, "origin": origin, "run": run,
                "origin_month": origin.strftime("%Y-%m"), "calendar_season": calendar_season(origin.month),
                "forecast_epoch": forecast_epoch(run),
            } for station in STATIONS)
            all_dates.append(origin)
    dates = pd.Series(all_dates)
    if not dates.is_monotonic_increasing or not dates.is_unique or dates.diff().dropna().lt(pd.Timedelta(hours=48)).any():
        raise ValueError("All splits must be chronological with disjoint targets")
    return pd.DataFrame(rows)


def load_protocol(path):
    protocol = json.loads(path.read_text())
    expected = {
        "schema_version": 1, "observation_policy": POLICY["version"], "target": "station_air_temperature_c",
        "station_ids": STATIONS, "horizon_hours": 48, "origin_utc_hour": 6, "origin_step_hours": 192,
        "history_policy": {"context_hours": 168, "max_gap_hours": 6, "max_imputed_fraction": 0.1},
        "minimum_target_hours": 1, "forecast_model": "ecmwf_ifs", "minimum_run_age_hours": 6,
        "forecast_epoch_boundary": "2026-05-12T06:00:00Z", "ridge_alpha": 10.0,
        "methods": pilot.METHODS,
        "selection": {"split": "development", "metric": "pooled_rmse_c", "candidates_in_tie_order": pilot.CANDIDATES, "refit_after_selection": False},
        "timesfm": {"model_id": MODEL_ID, "revision": MODEL_REVISION, "point_estimate": "p50", "fine_tuned": False},
    }
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"Unsupported seasonal protocol: {key}")
    scoring = protocol["scoring"]
    expected_scoring = {
        "primary_baseline": "ecmwf_ifs_c", "primary_metric": "pooled_rmse_c", "common_observation_mask": True,
        "lead_bands": [[1, 6], [7, 24], [25, 48]],
        "calendar_seasons": {"DJF": [12, 1, 2], "MAM": [3, 4, 5], "JJA": [6, 7, 8], "SON": [9, 10, 11]},
        "required_test_seasons": ["DJF", "MAM", "JJA"], "minimum_accepted_origin_dates_per_test_season": 6,
        "pilot_pass": {"minimum_accepted_origin_fraction": 0.8, "minimum_scored_hour_fraction": 0.75, "minimum_accepted_origins_per_station": 20, "selected_candidate_rmse_delta_upper_95ci_below_zero": True},
    }
    for key, value in expected_scoring.items():
        if scoring.get(key) != value:
            raise ValueError(f"Unsupported seasonal scoring: {key}")
    bootstrap = scoring["bootstrap"]
    if any(type(bootstrap.get(key)) is not int for key in ["replicates", "seed", "block_origins"]):
        raise ValueError("Bootstrap settings must be integers")
    if bootstrap["replicates"] < 100 or bootstrap["seed"] < 0 or not 1 <= bootstrap["block_origins"] <= protocol["splits"]["holdout"]["origins_per_station"]:
        raise ValueError("Invalid seasonal bootstrap settings")
    source_path = ROOT / protocol["source_snapshot"]
    if pilot.sha256(source_path) != protocol["source_snapshot_sha256"]:
        raise ValueError("Source inventory differs from the frozen snapshot")
    snapshot = json.loads(source_path.read_text())
    keys = [(row["station"], row["year"]) for row in snapshot["sources"]]
    wanted = {(station, year) for station in STATIONS for year in [2024, 2025, 2026]}
    if len(keys) != len(wanted) or set(keys) != wanted or snapshot["policy"] != POLICY or snapshot["stations"] != SOURCE_STATIONS:
        raise ValueError("Invalid source inventory or observation policy")
    schedule(protocol)
    return protocol, snapshot


def audit_origins(protocol, observations):
    rows = []
    for item in schedule(protocol).to_dict("records"):
        observed = observations[item["station"]]["observed_c"]
        origin = item["origin"]
        target = observed.reindex(pd.date_range(origin + pd.Timedelta(hours=1), periods=48, freq="h"))
        reason, detail, imputed = "", "", None
        try:
            prepared = prepare_history(observed, origin, context_hours=168)
            imputed = int(prepared["imputed"].sum())
        except ValueError as error:
            reason, detail = "history_policy", str(error)
        if not target.notna().any():
            reason = ";".join(filter(None, [reason, "no_verification_observations"]))
        rows.append(item | {
            "status": "data_rejected" if reason else "eligible", "reason": reason, "detail": detail,
            "imputed_history_hours": imputed, "target_observed_hours": int(target.notna().sum()), "scored_hours": 0,
        })
    return pd.DataFrame(rows)


def annotate(predictions, observations):
    frames = []
    for station, frame in predictions.groupby("station", sort=False):
        values = frame.copy()
        values["origin_month"] = values["origin"].dt.strftime("%Y-%m")
        values["calendar_season"] = values["origin"].dt.month.map(calendar_season)
        runs = values["origin"] - pd.Timedelta(hours=6)
        values["forecast_epoch"] = runs.map(forecast_epoch)
        values["guidance_crosses_ifs_update"] = (runs - pd.Timedelta(hours=192) < EPOCH_BOUNDARY) & (runs >= EPOCH_BOUNDARY)
        actual = observations[station].reindex(pd.DatetimeIndex(values["valid_time"]))
        for column in ["source_code", "quality_code", "report_type", "source_station_id"]:
            values[f"verification_{column}"] = actual[column].to_numpy()
        frames.append(values)
    return pd.concat(frames, ignore_index=True)


def seasonal_tables(ledger, predictions):
    tables = {}
    for key in ["origin_month", "calendar_season", "forecast_epoch", "verification_report_type", "verification_source_code"]:
        rows = []
        for value, group in predictions.groupby(key, dropna=False, sort=True):
            if group["scored"].any():
                rows.extend({key: value, "method": method, **score} for method, score in score_case(group, pilot.METHODS).items())
        tables[f"per_{key}.csv"] = pd.DataFrame(rows)
    coverage = []
    for (season, station), group in ledger.groupby(["calendar_season", "station"]):
        coverage.append({
            "calendar_season": season, "station": station, "planned_cases": len(group),
            "accepted_cases": int(group["status"].eq("scored").sum()),
            "data_rejected_cases": int(group["status"].eq("data_rejected").sum()),
            "scored_hours": int(group["scored_hours"].sum()), "planned_hours": len(group) * 48,
        })
    tables["season_coverage.csv"] = pd.DataFrame(coverage)
    return tables


def seasonal_decision(summary, comparison, ledger, protocol):
    decision = pilot.pilot_decision(summary, comparison, protocol)
    season_dates = ledger.loc[ledger["status"].eq("scored")].groupby("calendar_season")["origin"].nunique()
    decision["accepted_origin_dates_by_season"] = {season: int(count) for season, count in season_dates.items()}
    decision["checks"]["seasonal_date_coverage"] = all(
        season_dates.get(season, 0) >= protocol["scoring"]["minimum_accepted_origin_dates_per_test_season"]
        for season in protocol["scoring"]["required_test_seasons"]
    )
    decision["retrospective_pilot_pass"] = all(decision["checks"].values())
    return decision


def print_report(manifest):
    if manifest.get("results", {}).get("scores"):
        print(pd.DataFrame(manifest["results"]["scores"]).T.round(4).to_string())
    if "selected_candidate" in manifest:
        print("Development-selected candidate:", manifest["selected_candidate"])
    if "primary_comparison" in manifest:
        print(json.dumps(manifest["primary_comparison"], indent=2))
        print(json.dumps(manifest["decision"], indent=2))


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
    protocol, snapshot = load_protocol(args.protocol)
    hashes = pilot.pipeline_hashes() | {name: pilot.sha256(ROOT / "scripts" / name) for name in ["weather_seasonal.py", "weather_seasonal_data.py"]}
    identity = {
        "protocol_sha256": pilot.sha256(args.protocol), "source_snapshot_sha256": protocol["source_snapshot_sha256"],
        "pipeline_sha256": hashes,
        "runtime_versions": {name: importlib.metadata.version(name) for name in ["numpy", "pandas", "scikit-learn", "torch", "timesfm"]},
    }
    root = args.output_dir or ROOT / "results/weather" / protocol["id"]
    root.mkdir(parents=True, exist_ok=True)
    for filename, source in [("protocol.json", args.protocol), ("sources.json", ROOT / protocol["source_snapshot"])]:
        path = root / filename
        if path.exists() and pilot.sha256(path) != pilot.sha256(source):
            raise ValueError("Different frozen input in output directory")
        path.write_bytes(source.read_bytes())
    model, development = None, None
    if args.stage in ["development", "holdout"]:
        pilot.require_stage(root, "train", identity)
        identity["correction_model_sha256"] = pilot.sha256(root / "train/model.json")
        model = json.loads((root / "train/model.json").read_text())
    if args.stage == "holdout":
        development = pilot.require_stage(root, "development", identity)
        scores = score_case(pilot.read_frame(root / "development/predictions.csv"), pilot.METHODS)
        if development["selected_candidate"] != pilot.select_candidate({"scores": scores}):
            raise ValueError("Development selection does not match verified predictions")
    output = root / args.stage
    output.mkdir(parents=True, exist_ok=True)
    path = output / "manifest.json"
    if path.exists():
        previous = json.loads(path.read_text())
        if any(previous.get(key) != value for key, value in identity.items()):
            raise ValueError("Output belongs to different code/inputs; use a new output directory")
        if previous["run_status"] == "complete":
            pilot.verify_artifacts(output, previous["artifact_sha256"])
            print_report(previous)
            print(f"Already complete and verified: {output}")
            return
    manifest = identity | {"stage": args.stage, "run_status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "limits": protocol["limits"]}
    pilot.write_json(path, manifest)
    observations, provenance, _ = load_years(args.cache_dir, snapshot["sources"])
    ledger = audit_origins(protocol, observations)
    if args.stage != "audit":
        ledger = ledger.loc[ledger["split"].eq(args.stage)].copy()
    ledger.to_csv(output / "origins.csv", index=False)
    manifest["observations"] = provenance
    if args.stage == "audit":
        manifest |= {"run_status": "complete", "computes_forecast_scores": False, "artifact_sha256": pilot.artifact_hashes(output)}
        pilot.write_json(path, manifest)
        print(ledger.groupby(["split", "station", "status"]).size().to_string())
        return
    ledger, predictions = pilot.collect(
        protocol, ledger, {station: frame["observed_c"] for station, frame in observations.items()}, provenance, output, identity, args, model,
    )
    complete = ledger["status"].isin(["ready", "scored", "data_rejected"]).all() and not predictions.empty
    if not predictions.empty:
        predictions = annotate(predictions, observations)
        predictions.to_csv(output / "predictions.csv", index=False)
    if args.stage == "train" and complete:
        fitted = fit_correction(predictions, utc_hour(protocol["fit_cutoff"]), protocol["ridge_alpha"])
        fitted |= {"training_predictions_sha256": pilot.sha256(output / "predictions.csv"), "source_snapshot_sha256": protocol["source_snapshot_sha256"]}
        pilot.write_json(output / "model.json", fitted)
        print(f"Fitted on {fitted['training_matched_hours']} matched hours across twelve origin months.")
    elif args.stage != "train":
        summary, tables = pilot.summarize(ledger, predictions, protocol)
        if not predictions.empty:
            tables |= seasonal_tables(ledger, predictions)
        for filename, table in tables.items():
            table.to_csv(output / filename, index=False)
        manifest["results"] = summary
        if complete and args.stage == "development":
            manifest["selected_candidate"] = pilot.select_candidate(summary)
        elif complete:
            candidate = development["selected_candidate"]
            comparison = pilot.paired_bootstrap(predictions, ledger["origin"], candidate, protocol["scoring"]["bootstrap"])
            comparison["interpretation"] = "Negative RMSE delta favors the candidate on the fixed seasonal sample; not an operational or India-wide validation."
            decision = seasonal_decision(summary, comparison, ledger, protocol)
            manifest |= {"selected_candidate": candidate, "primary_comparison": comparison, "decision": decision}
    manifest |= {"run_status": "complete" if complete else "incomplete", "finished_at": datetime.now(timezone.utc).isoformat(), "artifact_sha256": pilot.artifact_hashes(output)}
    pilot.write_json(path, manifest)
    print_report(manifest)
    print(f"{args.stage}: {ledger['status'].value_counts().to_dict()}; {manifest['run_status']}; {output}")
    if not complete:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
