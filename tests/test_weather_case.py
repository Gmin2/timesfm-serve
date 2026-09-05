import csv
import io
import json

import pandas as pd
import pytest
import requests

from scripts.weather_case import METHODS, STATION, build_case, fetch, parse_forecast, parse_observations, score_case, utc_hour


def observations(*rows):
    defaults = {
        "STATION": STATION,
        "DATE": "2025-07-01T00:00:00",
        "TMP": "+0280,1",
        "LATITUDE": "26.106092",
        "LONGITUDE": "91.585939",
        "ELEVATION": "49.37",
        "NAME": "GUWAHATI",
        "REPORT_TYPE": "FM-15",
    }
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=defaults)
    writer.writeheader()
    for row in rows:
        writer.writerow(defaults | row)
    return output.getvalue().encode()


def test_temperature_scaling_and_quality():
    data = observations(
        {},
        {"DATE": "2025-07-01T01:00:00", "TMP": "-0025,5"},
        {"DATE": "2025-07-01T02:00:00", "TMP": "+9999,1"},
        {"DATE": "2025-07-01T03:00:00", "TMP": "+0300,3"},
        {"DATE": "2025-07-01T04:00:00", "TMP": "broken"},
        {"DATE": "2025-07-01T05:00:00", "TMP": "+0300,0"},
    )
    hourly, station, audit = parse_observations(data)
    assert hourly.tolist() == [28.0, -2.5]
    assert str(hourly.index.tz) == "UTC"
    assert station["elevation_m"] == 49.37
    assert audit["rejected_temperature_rows"] == 4


def test_synoptic_and_off_hour_reports_are_not_mixed_into_metar():
    hourly, _, audit = parse_observations(observations(
        {},
        {"TMP": "+0282,1", "REPORT_TYPE": "FM-12"},
        {"DATE": "2025-07-01T00:30:00", "TMP": "+0300,1"},
    ))
    assert hourly.tolist() == [28.0]
    assert audit["conflicting_hours"] == 0
    assert audit["excluded_report_type_rows"] == 1
    assert audit["off_hour_rows"] == 1


def test_identical_duplicates_collapse_and_conflicts_stay_missing():
    hourly, _, audit = parse_observations(observations(
        {}, {},
        {"DATE": "2025-07-01T01:00:00", "TMP": "+0300,1"},
        {"DATE": "2025-07-01T01:00:00", "TMP": "+0310,1"},
    ))
    assert len(hourly) == 2
    assert hourly.iloc[0] == 28.0
    assert pd.isna(hourly.iloc[1])
    assert audit["duplicate_exact_hour_rows"] == 2
    assert audit["conflicting_hours"] == 1


@pytest.mark.parametrize("row", [{"STATION": "another"}, {"LATITUDE": "25"}])
def test_station_identity_and_moves_are_checked(row):
    with pytest.raises(ValueError):
        parse_observations(observations({}, row))


@pytest.mark.parametrize("value", ["2025-07-01T00:00", "2025-07-01T00:30:00Z", "NaT"])
def test_origin_requires_explicit_timezone_and_exact_hour(value):
    with pytest.raises(ValueError):
        utc_hour(value)


def test_origin_is_converted_to_utc():
    assert utc_hour("2025-07-01T11:30:00+05:30") == pd.Timestamp("2025-07-01T06:00:00Z")


def forecast_payload(**updates):
    data = {
        "latitude": 26.115992,
        "longitude": 91.57722,
        "elevation": 49.37,
        "utc_offset_seconds": 0,
        "hourly_units": {"temperature_2m": "\N{DEGREE SIGN}C"},
        "hourly": {"time": ["2025-07-01T00:00", "2025-07-01T01:00"], "temperature_2m": [28.1, 29.2]},
    }
    return json.dumps(data | updates).encode()


def test_single_run_forecast_parses_real_response_shape():
    forecast, grid = parse_forecast(forecast_payload(), utc_hour("2025-07-01T00:00Z"))
    assert forecast.tolist() == [28.1, 29.2]
    assert grid["elevation"] == 49.37
    assert forecast.index[-1] == utc_hour("2025-07-01T01:00Z")


@pytest.mark.parametrize("updates", [
    {"utc_offset_seconds": 19800},
    {"hourly_units": {"temperature_2m": "K"}},
    {"hourly": {"time": ["2025-07-01T01:00"], "temperature_2m": [28.0]}},
    {"hourly": {"time": ["2025-07-01T00:00", "2025-07-01T00:00"], "temperature_2m": [28.0, 28.0]}},
    {"hourly": {"time": ["2025-07-01T00:00", "2025-07-01T02:00"], "temperature_2m": [28.0, 28.0]}},
    {"hourly": {"time": ["2025-07-01T00:00"], "temperature_2m": [float("inf")]}},
])
def test_forecast_units_cadence_and_run_alignment_are_validated(updates):
    with pytest.raises(ValueError):
        parse_forecast(forecast_payload(**updates), utc_hour("2025-07-01T00:00Z"))


def case_inputs():
    origin = utc_hour("2025-07-01T06:00Z")
    index = pd.date_range(start=origin - pd.Timedelta(hours=23), periods=72, freq="h")
    observed = pd.Series(range(72), index=index, dtype=float)
    forecast = pd.Series(30.0, index=index)
    return observed, forecast, origin


def test_baselines_only_use_history_even_for_the_second_forecast_day():
    observed, forecast, origin = case_inputs()
    first = build_case(observed, forecast, origin)
    observed.loc[observed.index > origin] = 999.0
    second = build_case(observed, forecast, origin)
    pd.testing.assert_frame_equal(first[METHODS], second[METHODS])
    assert first["yesterday_c"].tolist() == list(range(24)) * 2
    assert first["persistence_c"].eq(23.0).all()
    assert first.index[0] == origin + pd.Timedelta(hours=1)
    assert first.index[-1] == origin + pd.Timedelta(hours=48)


def test_missing_targets_remain_missing_and_all_models_use_the_same_cases():
    observed, forecast, origin = case_inputs()
    observed.loc[origin + pd.Timedelta(hours=4)] = float("nan")
    case = build_case(observed, forecast, origin)
    assert pd.isna(case["observed_c"].iloc[3])
    assert not case["scored"].iloc[3]
    assert case["scored"].sum() == 47
    assert {score["n"] for score in score_case(case).values()} == {47}


def test_missing_past_day_prevents_seasonal_baseline():
    observed, forecast, origin = case_inputs()
    observed.loc[origin - pd.Timedelta(hours=5)] = float("nan")
    with pytest.raises(ValueError, match="previous 24"):
        build_case(observed, forecast, origin)


def test_missing_forecast_is_not_filled_from_another_run():
    observed, forecast, origin = case_inputs()
    forecast.loc[origin + pd.Timedelta(hours=10)] = float("nan")
    with pytest.raises(ValueError, match="complete prediction horizon"):
        build_case(observed, forecast, origin)


def test_metrics_use_celsius_errors_and_matched_denominator():
    case = pd.DataFrame({
        "observed_c": [10.0, 12.0, None],
        "persistence_c": [8.0, 8.0, 8.0],
        "yesterday_c": [11.0, 11.0, 11.0],
        "ecmwf_ifs_c": [10.0, 13.0, 14.0],
        "scored": [True, True, False],
    })
    scores = score_case(case)
    assert scores["persistence_c"] == pytest.approx({"n": 2, "mae_c": 3.0, "rmse_c": 10**0.5, "bias_c": -3.0})
    assert scores["yesterday_c"]["mae_c"] == 1.0
    assert scores["ecmwf_ifs_c"]["rmse_c"] == pytest.approx(0.5**0.5)
    case["scored"] = False
    with pytest.raises(ValueError, match="No matched"):
        score_case(case)


def test_cache_reuses_identical_requests_and_checks_saved_bytes(tmp_path, monkeypatch):
    calls = []

    def get(url, timeout):
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = b"source bytes"
        return response

    monkeypatch.setattr(requests, "get", get)
    first = fetch("https://example.test/weather", tmp_path, {"run": "00", "model": "ifs"})
    assert fetch("https://example.test/weather", tmp_path, {"model": "ifs", "run": "00"}) == first
    assert len(calls) == 1
    assert first[1]["bytes"] == len(first[0])
    next(tmp_path.glob("*.raw")).write_bytes(b"changed")
    with pytest.raises(ValueError, match="Cache verification failed"):
        fetch("https://example.test/weather", tmp_path, {"run": "00", "model": "ifs"})


def test_failed_download_is_not_cached(tmp_path, monkeypatch):
    def get(url, timeout):
        response = requests.Response()
        response.status_code = 404
        response._content = b"not found"
        return response

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(requests.HTTPError):
        fetch("https://example.test/missing", tmp_path)
    assert list(tmp_path.iterdir()) == []
