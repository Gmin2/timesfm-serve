"""Capture fresh, auditable weather inputs. Never runs on an API read."""

import hashlib
import json
import math
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pandas as pd
import requests

from scripts.weather_case import parse_forecast
from scripts.weather_conditioned import validate_inputs
from scripts.weather_model import MODEL_ID, MODEL_REVISION, prepare_history
from timesfm_serve import weather_catalog
from timesfm_serve.weather_live_policy import (
    MAX_OBSERVATION_LAG,
    POLICY,
    SOURCE_CODE,
    STATIONS,
    LiveInputError,
    check_issue_window,
    current_origin,
    parse_utc,
    utc_now,
)

METAR_URL = "https://aviationweather.gov/api/data/metar"
NWP_URL = "https://single-runs-api.open-meteo.com/v1/forecast"


def metar_parameters(station, end):
    return {"ids": station["icao"], "format": "json", "hours": 48, "date": end.strftime("%Y-%m-%dT%H:%M:%SZ")}


def nwp_parameters(station, run):
    return {
        "latitude": station["latitude"], "longitude": station["longitude"], "elevation": station["elevation_m"],
        "hourly": "temperature_2m", "models": "ecmwf_ifs", "run": run.strftime("%Y-%m-%dT%H:%M"),
        "forecast_hours": 55, "timezone": "UTC", "temperature_unit": "celsius",
    }


def verify_source_request(source, endpoint, expected):
    parsed, target = urlsplit(source["url"]), urlsplit(endpoint)
    query = {key: [str(value)] for key, value in expected.items()}
    if (parsed.scheme, parsed.netloc, parsed.path) != (target.scheme, target.netloc, target.path):
        raise LiveInputError("source_endpoint_mismatch")
    if source["parameters"] != expected or parse_qs(parsed.query, keep_blank_values=True) != query:
        raise LiveInputError("source_parameters_mismatch")


class ProviderError(RuntimeError):
    pass


class SourceClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "timesfm-weather-pilot/0.2 (three-station research ingestion)"
        self.next_request = 0.0

    def close(self):
        self.session.close()

    def fetch(self, provider, url, params):
        if url not in (METAR_URL, NWP_URL):
            raise ValueError("Unapproved source URL")
        time.sleep(max(0, self.next_request - time.monotonic()))
        started = time.monotonic()
        try:
            with self.session.get(url, params=params, timeout=(5, 20), stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise ProviderError(f"{provider}_http_{response.status_code}")
                raw = bytearray()
                for chunk in response.iter_content(32768):
                    if len(raw) + len(chunk) > 1024 * 1024 or time.monotonic() - started > 60:
                        raise ProviderError(f"{provider}_response_limit")
                    raw.extend(chunk)
                received_at = utc_now()
                body = bytes(raw).decode("utf-8")
                json.loads(body)
                return {
                    "provider": provider, "url": response.url, "parameters": params,
                    "received_at": received_at.isoformat(), "sha256": hashlib.sha256(raw).hexdigest(),
                    "body": body,
                }
        except (requests.RequestException, UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"{provider}_unavailable") from exc
        finally:
            self.next_request = time.monotonic() + 1


def parse_metars(sources, station, origin):
    reports, rejected = {}, Counter()
    raw_rows = 0
    for source in sources:
        rows = json.loads(source["body"])
        if not isinstance(rows, list) or len(rows) >= 400:
            raise LiveInputError("metar_response_shape_or_truncation")
        received_at = parse_utc(source["received_at"])
        end = parse_utc(source["parameters"]["date"])
        start = end - timedelta(hours=48)
        for row in rows:
            raw_rows += 1
            if not isinstance(row, dict) or row.get("icaoId") != station["icao"]:
                raise LiveInputError("metar_station_mismatch")
            try:
                lat, lon = float(row["lat"]), float(row["lon"])
                if not math.isfinite(lat + lon) or abs(lat - station["latitude"]) > 0.03 or abs(lon - station["longitude"]) > 0.03:
                    raise LiveInputError("metar_location_mismatch")
                epoch = row["obsTime"]
                if isinstance(epoch, bool) or not isinstance(epoch, int):
                    raise LiveInputError("invalid_observation_time")
                observed_at = datetime.fromtimestamp(epoch, timezone.utc)
                receipt = parse_utc(row["receiptTime"])
                if not start <= observed_at <= end or observed_at > origin or not observed_at <= receipt <= received_at:
                    raise LiveInputError("metar_time_mismatch")
            except (KeyError, TypeError, OverflowError) as exc:
                raise LiveInputError("invalid_metar_metadata") from exc
            if epoch % 3600 or row.get("metarType") != "METAR":
                rejected["off_hour_or_non_metar"] += 1
                continue
            value = row.get("temp")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not -90 <= value <= 60:
                rejected["invalid_temperature"] += 1
                continue
            reports.setdefault(observed_at, set()).add(float(value))
    # Disagreement is missing data, not an arbitrary choice of a corrected report.
    observations = pd.Series({t: next(iter(values)) if len(values) == 1 else np.nan for t, values in reports.items()}, dtype=float)
    observations.index = pd.DatetimeIndex(observations.index, tz="UTC")
    try:
        history = prepare_history(observations.sort_index(), pd.Timestamp(origin), context_hours=168)
    except ValueError as exc:
        raise LiveInputError("observation_history_incomplete") from exc
    if origin - history["source_time"].iloc[-1] > MAX_OBSERVATION_LAG:
        raise LiveInputError("latest_observation_stale")
    return history, {
        "policy": POLICY, "quality_scope": "structural_checks_only_not_GHCNh_quality_control",
        "raw_rows": raw_rows, "rejected_rows": dict(rejected),
        "conflicting_hours": sum(len(v) > 1 for v in reports.values()),
        "imputed_hours": int(history["imputed"].sum()),
        "latest_observation_at": history["source_time"].iloc[-1].isoformat(),
        "qcField_interpretation": "Not documented in the JSON schema; retained in source bodies, not treated as a quality pass flag",
    }


def build_guidance(sources, station, origin):
    pieces = []
    for source in sources:
        run = pd.Timestamp(source["parameters"]["run"], tz="UTC")
        segment = run + pd.Timedelta(hours=6)
        forecast, grid = parse_forecast(source["body"], run)
        if len(forecast) != 55:
            raise LiveInputError("forecast_horizon_mismatch")
        if not all(math.isfinite(float(v)) for v in grid.values()):
            raise LiveInputError("invalid_forecast_grid")
        if abs(grid["latitude"] - station["latitude"]) > 0.2 or abs(grid["longitude"] - station["longitude"]) > 0.2 or grid["elevation"] != station["elevation_m"]:
            raise LiveInputError("forecast_location_mismatch")
        index = pd.date_range(segment + pd.Timedelta(hours=1), periods=48, freq="h", name="valid_time")
        pieces.append(pd.DataFrame({"nwp_c": forecast.reindex(index), "segment_origin": segment, "run": run, "run_lead_hours": range(7, 55)}, index=index))
    expected = pd.date_range(pd.Timestamp(origin) - pd.Timedelta(hours=167), periods=216, freq="h", name="valid_time")
    return pd.concat(pieces).reindex(expected)


def capture(station_id, origin=None, client=None):
    station = STATIONS[station_id]
    origin = parse_utc(origin) if origin is not None else current_origin(utc_now())
    check_issue_window(origin, utc_now())
    own_client = client is None
    client = client or SourceClient()
    observations, forecasts = [], []
    try:
        for offset in (0, 48, 96, 144):
            end = origin - timedelta(hours=offset)
            observations.append(client.fetch("noaa_awc", METAR_URL, metar_parameters(station, end)))
        history, audit = parse_metars(observations, station, origin)
        for offset in (192, 144, 96, 48, 0):
            run = origin - timedelta(hours=offset + 6)
            forecasts.append(client.fetch("open_meteo_ecmwf_ifs", NWP_URL, nwp_parameters(station, run)))
        guidance = build_guidance(forecasts, station, origin)
        context, covariate = validate_inputs(history, guidance)
        now = utc_now()
        check_issue_window(origin, now)
        return {
            "schema_version": 1, "station_id": station_id, "forecast_origin": origin.isoformat(),
            "captured_at": now.isoformat(), "policy": POLICY, "model_id": MODEL_ID, "model_revision": MODEL_REVISION,
            "observation_audit": audit, "history_policy": history.attrs["history_policy"],
            "context_float32_sha256": hashlib.sha256(context.astype("<f4").tobytes()).hexdigest(),
            "covariate_float32_sha256": hashlib.sha256(covariate.astype("<f4").tobytes()).hexdigest(),
            "inference_source_sha256": {name: hashlib.sha256((weather_catalog.ROOT / "scripts" / name).read_bytes()).hexdigest() for name in SOURCE_CODE},
            "sources": observations + forecasts,
        }
    finally:
        if own_client:
            client.close()


def reconstruct(document):
    if document["schema_version"] != 1 or document["policy"] != POLICY or document["model_id"] != MODEL_ID or document["model_revision"] != MODEL_REVISION:
        raise LiveInputError("live_input_version_mismatch")
    station, origin = STATIONS[document["station_id"]], parse_utc(document["forecast_origin"])
    observations, forecasts = [], []
    captured_at = parse_utc(document["captured_at"])
    check_issue_window(origin, captured_at)
    for name in SOURCE_CODE:
        if hashlib.sha256((weather_catalog.ROOT / "scripts" / name).read_bytes()).hexdigest() != document["inference_source_sha256"][name]:
            raise LiveInputError("live_inference_code_changed")
    for source in document["sources"]:
        if hashlib.sha256(source["body"].encode()).hexdigest() != source["sha256"] or parse_utc(source["received_at"]) > captured_at:
            raise LiveInputError("source_integrity_failure")
        if source["provider"] == "noaa_awc":
            observations.append(source)
        elif source["provider"] == "open_meteo_ecmwf_ifs":
            forecasts.append(source)
        else:
            raise LiveInputError("unknown_source")
    if len(observations) != 4 or len(forecasts) != 5:
        raise LiveInputError("incomplete_source_bundle")
    for source, offset in zip(observations, (0, 48, 96, 144), strict=True):
        verify_source_request(source, METAR_URL, metar_parameters(station, origin - timedelta(hours=offset)))
    for source, offset in zip(forecasts, (192, 144, 96, 48, 0), strict=True):
        verify_source_request(source, NWP_URL, nwp_parameters(station, origin - timedelta(hours=offset + 6)))
    history, audit = parse_metars(observations, station, origin)
    guidance = build_guidance(forecasts, station, origin)
    context, covariate = validate_inputs(history, guidance)
    if audit != document["observation_audit"] or history.attrs["history_policy"] != document["history_policy"]:
        raise LiveInputError("history_audit_mismatch")
    for name, values in (("context", context), ("covariate", covariate)):
        if hashlib.sha256(values.astype("<f4").tobytes()).hexdigest() != document[f"{name}_float32_sha256"]:
            raise LiveInputError("live_input_hash_mismatch")
    return history, guidance
