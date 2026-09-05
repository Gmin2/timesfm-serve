"""Build one auditable hourly temperature forecast case, separate from demand."""

import argparse
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

STATION = "42410099999"
ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "weather" / "raw"
NOAA = "https://www.ncei.noaa.gov/data/global-hourly/access"
SINGLE_RUNS = "https://single-runs-api.open-meteo.com/v1/forecast"
SINGLE_RUNS_DOCS = "https://open-meteo.com/en/docs/single-runs-api"
METHODS = ["persistence_c", "yesterday_c", "ecmwf_ifs_c"]


def fetch(url, cache_dir, params=None):
    """Cache source bytes and retrieval provenance; reject changed cache bytes."""
    request_url = requests.Request("GET", url, params=sorted((params or {}).items())).prepare().url
    key = hashlib.sha256(request_url.encode()).hexdigest()
    body_path = cache_dir / f"{key}.raw"
    meta_path = cache_dir / f"{key}.json"
    if body_path.exists() and meta_path.exists():
        body = body_path.read_bytes()
        meta = json.loads(meta_path.read_text())
        if meta["sha256"] != hashlib.sha256(body).hexdigest() or meta["url"] != request_url:
            raise ValueError(f"Cache verification failed: {body_path}")
        return body, meta

    response = requests.get(request_url, timeout=60)
    response.raise_for_status()
    body = response.content
    meta = {
        "url": request_url,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    body_path.write_bytes(body)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return body, meta


def parse_observations(body, station=STATION):
    columns = ["STATION", "DATE", "TMP", "LATITUDE", "LONGITUDE", "ELEVATION", "NAME", "REPORT_TYPE"]
    frame = pd.read_csv(io.BytesIO(body), usecols=columns, dtype=str, keep_default_na=False)
    if frame.empty or set(frame["STATION"]) != {station}:
        raise ValueError("Expected observations from exactly the requested station")
    positions = frame[["LATITUDE", "LONGITUDE", "ELEVATION"]].drop_duplicates()
    if len(positions) != 1:
        raise ValueError("Station coordinates/elevation changed; inspect before combining records")
    metadata = {
        "id": station,
        "name": frame["NAME"].iloc[0],
        "latitude": float(positions["LATITUDE"].iloc[0]),
        "longitude": float(positions["LONGITUDE"].iloc[0]),
        "elevation_m": float(positions["ELEVATION"].iloc[0]),
    }
    dates = pd.to_datetime(frame["DATE"], utc=True, errors="raise")
    parts = frame["TMP"].str.extract(r"^([+-]\d{4}),([0-9A-Z])$")
    temperature = pd.to_numeric(parts[0], errors="coerce")
    # ISD codes 1 and 5 passed all quality checks. Be conservative for this pilot.
    accepted = parts[1].isin(["1", "5"]) & temperature.between(-932, 618)
    exact_hour = dates.eq(dates.dt.floor("h"))
    # Do not mix rounded METAR temperatures with more precise synoptic reports.
    report_type = frame["REPORT_TYPE"].eq("FM-15")
    selected = accepted & exact_hour & report_type
    good = pd.DataFrame({"time": dates[selected], "temperature_c": temperature[selected] / 10})
    groups = good.groupby("time")["temperature_c"]
    distinct = groups.nunique()
    hourly = groups.first().where(distinct.eq(1)).sort_index()
    hourly.index.name = "valid_time"
    audit = {
        "raw_rows": len(frame),
        "rejected_temperature_rows": int((~accepted).sum()),
        "off_hour_rows": int((~exact_hour).sum()),
        "excluded_report_type_rows": int((~report_type).sum()),
        "report_type": "FM-15",
        "accepted_quality_codes": ["1", "5"],
        "accepted_exact_hour_rows": len(good),
        "duplicate_exact_hour_rows": int(len(good) - len(distinct)),
        "conflicting_hours": int(distinct.gt(1).sum()),
        "first_report": dates.min().isoformat(),
        "last_report": dates.max().isoformat(),
    }
    return hourly, metadata, audit


def utc_hour(value):
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("Timestamp must include a timezone, for example 2025-07-01T06:00:00Z")
    result = result.tz_convert("UTC")
    if result != result.floor("h"):
        raise ValueError("Timestamp must be on an exact UTC hour")
    return result


def parse_forecast(body, run):
    data = json.loads(body)
    if data.get("utc_offset_seconds") != 0 or data.get("hourly_units", {}).get("temperature_2m") != "\N{DEGREE SIGN}C":
        raise ValueError("Expected a UTC forecast in degrees Celsius")
    hourly = data["hourly"]
    index = pd.to_datetime(hourly["time"], utc=True, errors="raise")
    expected = pd.date_range(start=run, periods=len(index), freq="h")
    if len(index) == 0 or not index.equals(expected):
        raise ValueError("Forecast must contain consecutive hourly timestamps starting at the requested run")
    series = pd.Series(pd.to_numeric(hourly["temperature_2m"], errors="raise"), index=index, name="ecmwf_ifs_c")
    if series.dropna().isin([float("inf"), float("-inf")]).any():
        raise ValueError("Forecast contains non-finite temperatures")
    grid = {key: data[key] for key in ["latitude", "longitude", "elevation"]}
    return series, grid


def load_forecast(station, run, origin, cache_dir=CACHE, horizon=48):
    end = origin + pd.Timedelta(hours=horizon)
    forecast_hours = int((end - run) / pd.Timedelta(hours=1)) + 1
    body, source = fetch(SINGLE_RUNS, cache_dir, {
        "latitude": station["latitude"],
        "longitude": station["longitude"],
        "elevation": station["elevation_m"],
        "hourly": "temperature_2m",
        "models": "ecmwf_ifs",
        "run": run.strftime("%Y-%m-%dT%H:%M"),
        "forecast_hours": forecast_hours,
        "timezone": "UTC",
        "temperature_unit": "celsius",
    })
    forecast, grid = parse_forecast(body, run)
    return forecast, grid, source


def build_case(observed, forecast, origin, horizon=48):
    index = pd.date_range(start=origin + pd.Timedelta(hours=1), periods=horizon, freq="h", name="valid_time")
    past_day = observed.reindex(pd.date_range(end=origin, periods=24, freq="h"))
    if past_day.isna().any():
        raise ValueError("The previous 24 exact-hour observations must be complete for both simple baselines")
    prediction = forecast.reindex(index)
    if prediction.isna().any():
        raise ValueError("The selected forecast run does not cover the complete prediction horizon")
    case = pd.DataFrame({
        "lead_hours": range(1, horizon + 1),
        "observed_c": observed.reindex(index),
        "persistence_c": float(past_day.iloc[-1]),
        "yesterday_c": [float(past_day.iloc[i % 24]) for i in range(horizon)],
        "ecmwf_ifs_c": prediction,
    }, index=index)
    case["scored"] = case[["observed_c", *METHODS]].notna().all(axis=1)
    return case


def score_case(case, methods=None):
    matched = case.loc[case["scored"]]
    if matched.empty:
        raise ValueError("No matched observations to score")
    scores = {}
    for method in METHODS if methods is None else methods:
        error = matched[method] - matched["observed_c"]
        scores[method] = {
            "n": len(error),
            "mae_c": float(error.abs().mean()),
            "rmse_c": float((error.pow(2).mean()) ** 0.5),
            "bias_c": float(error.mean()),
        }
    return scores


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="2025-07-01T06:00:00Z")
    parser.add_argument("--run", help="ECMWF initialization time; default is the latest six-hour cycle at least six hours old")
    parser.add_argument("--cache-dir", type=Path, default=CACHE)
    parser.add_argument("--output-dir", type=Path, help="Default: results/weather/<station>_<origin>_<run>")
    parser.add_argument("--timesfm", action="store_true", help="Add a history-only TimesFM forecast with audited input filling")
    parser.add_argument("--device", choices=["cpu", "mps", "cuda"], default="cpu")
    parser.add_argument("--model-cache-dir", type=Path, help="Hugging Face hub cache; otherwise use its normal environment/default")
    parser.add_argument("--offline-model", action="store_true", help="Require the pinned model to exist in the cache")
    args = parser.parse_args()
    origin = utc_hour(args.origin)
    run = utc_hour(args.run) if args.run else origin.floor("6h") - pd.Timedelta(hours=6)
    if run.hour % 6 != 0 or run > origin - pd.Timedelta(hours=6):
        raise ValueError("Use an ECMWF six-hour run cycle at least six hours before the origin")
    history_index = pd.date_range(end=origin, periods=28 * 24, freq="h")
    target_index = pd.date_range(start=origin + pd.Timedelta(hours=1), periods=48, freq="h")
    if history_index[0].year != target_index[-1].year:
        raise ValueError("This first slice supports windows within one calendar year")
    body, source = fetch(f"{NOAA}/{origin.year}/{STATION}.csv", args.cache_dir)
    observed, station, audit = parse_observations(body)
    history = observed.reindex(history_index)
    forecast, grid, forecast_source = load_forecast(station, run, origin, args.cache_dir)
    case = build_case(observed, forecast, origin)
    case.insert(1, "run_lead_hours", [int((time - run) / pd.Timedelta(hours=1)) for time in case.index])
    methods = METHODS.copy()
    model_history = None
    model_metadata = None
    if args.timesfm:
        from scripts.weather_model import predict_timesfm, prepare_history, score_uncertainty

        model_history = prepare_history(observed, origin)
        print(f"Running TimesFM on {args.device}: {len(model_history)} history hours, {model_history['imputed'].sum()} imputed", flush=True)
        predictions, model_metadata = predict_timesfm(
            model_history, case.index, device=args.device, cache_dir=args.model_cache_dir, local_files_only=args.offline_model,
        )
        case = case.join(predictions)
        methods.append("timesfm_c")
    scores = score_case(case, methods)
    hindcast = run < pd.Timestamp("2026-05-12T06:00:00Z")
    manifest = {
        "schema_version": 1,
        "stage": "single_case_timesfm_history" if args.timesfm else "single_case_baselines_only",
        "station": station,
        "origin": origin.isoformat(),
        "observation_source": source,
        "observation_audit": audit,
        "history_hours": len(history),
        "history_observed_hours": int(history.notna().sum()),
        "history_missing_times": history.index[history.isna()].astype(str).tolist(),
        "target_hours": len(case),
        "target_observed_hours": int(case["observed_c"].notna().sum()),
        "target_start": case.index[0].isoformat(),
        "target_end": case.index[-1].isoformat(),
        "forecast_source": forecast_source,
        "forecast_model": "ecmwf_ifs",
        "forecast_run": run.isoformat(),
        "forecast_grid": grid,
        "forecast_kind": "hindcast" if hindcast else "archived_run",
        "forecast_vintage_basis": SINGLE_RUNS_DOCS,
        "assumed_minimum_run_age_hours": 6,
        "operational_availability_verified": False,
        "observation_availability_verified": False,
        "scores": scores,
        "limitations": [
            "One retrospective case is a data/benchmark smoke test, not evidence of general forecast skill.",
            "No NWP covariates or bias correction are used in this TimesFM run." if args.timesfm else "No TimesFM prediction or bias correction has been run in this slice.",
            "Imputation affects model inputs only; raw observations and verification targets remain unchanged."
            if args.timesfm else "History gaps remain missing; run with --timesfm to apply the explicit model-input policy.",
            "Only exact-hour FM-15 METAR reports with quality code 1 or 5 are used; temperatures are often rounded to whole degrees.",
            "Station sensor height has not been verified; station air temperature and grid 2m temperature are not identical measurements.",
            "ECMWF values are served through Open-Meteo with station-elevation adjustment, not unprocessed native-grid values.",
            "The run age is an explicit alignment assumption; historical publication and observation receipt times are unknown.",
            "Earlier IFS runs are labelled Cycle 49R1 hindcasts by the source, not proven historical operational forecasts."
            if hindcast else "The archive alone does not prove when this run was operationally available.",
        ],
    }
    if args.timesfm:
        manifest["schema_version"] = 2
        manifest["timesfm"] = model_metadata
        manifest["timesfm_uncertainty"] = score_uncertainty(case)
        manifest["limitations"].extend([
            "Input gap limits are pilot engineering choices, not validated weather-optimal settings. The imputation mask is saved but not passed to TimesFM.",
            "The 2025 case is retrospective; exclusion of these dates from TimesFM pretraining is unverified.",
            "Uncertainty coverage on one overlapping 48-hour trajectory does not establish calibration.",
        ])
    output = args.output_dir or ROOT / "results" / "weather" / f"guwahati_{origin:%Y%m%dT%H%MZ}_run_{run:%Y%m%dT%H%MZ}"
    if args.timesfm and args.output_dir is None:
        output = output / "timesfm_history"
    output.mkdir(parents=True, exist_ok=True)
    history.rename("observed_c").to_csv(output / "history.csv", index_label="valid_time")
    case.to_csv(output / "forecast.csv", float_format=None if args.timesfm else "%.4f")
    if model_history is not None:
        model_history.to_csv(output / "model_input.csv")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    print(f"Station: {station['name']}")
    print(f"Origin: {origin.isoformat()}; run: {run.isoformat()} ({manifest['forecast_kind']})")
    print(f"Observed history: {history.notna().sum()}/{len(history)}; verification: {case['scored'].sum()}/{len(case)}")
    print(pd.DataFrame(scores).T.round(3).to_string())
    if args.timesfm:
        print("TimesFM uncertainty:", json.dumps(manifest["timesfm_uncertainty"]))
    print(f"Artifacts: {output}")
    print("Single-case check only, not a general skill benchmark. Operational availability is not verified.")


if __name__ == "__main__":
    main()
