import numpy as np
import pandas as pd
import pytest

from scripts import weather_seasonal_data as data
from scripts.weather_seasonal_data import COLUMNS, STATIONS, parse_ghcnh


def record(**changes):
    row = dict.fromkeys(COLUMNS, "")
    row.update({
        "STATION": "INU042410-1", "Station_name": "GAUHATI", "DATE": "2026-01-01T00:00:00",
        "LATITUDE": "26.1", "LONGITUDE": "91.58", "ELEVATION": "54.0", "temperature": "12.3",
        "temperature_Quality_Code": "1", "temperature_Report_Type": "FM12", "temperature_Source_Code": "223",
        "temperature_Source_Station_ID": "ICAO-VEGT",
    })
    return row | changes


def encoded(rows):
    return pd.DataFrame(rows).to_csv(index=False, sep="|").encode()


def test_temperature_is_already_scaled_and_can_be_negative():
    frame, metadata, audit = parse_ghcnh(encoded([record(temperature="-12.3")]), STATIONS[0], 2026)
    assert frame["observed_c"].iloc[0] == -12.3
    assert metadata["id"] == "42410099999"
    assert metadata["ghcnh_id"] == "INU042410-1"
    assert audit["accepted_unique_hours"] == 1


@pytest.mark.parametrize("source,quality,report,accepted", [
    ("223", "1", "FM12", True), ("223", "1", "FM15", True),
    ("223", "4", "FM15", False), ("223", "2", "FM15", False),
    ("223", "", "FM15", False), ("223", "1", "FM16", False),
    ("413", "", "FM15", True), ("413", "s", "FM15", False),
    ("413", "1", "FM15", False), ("413", "", "FM16", False),
    ("412", "", "FM94_1", True), ("412", "", "FM12", True),
    ("999", "1", "FM15", False),
])
def test_quality_codes_are_interpreted_by_source(source, quality, report, accepted):
    frame, _, audit = parse_ghcnh(encoded([record(temperature_Source_Code=source, temperature_Quality_Code=quality, temperature_Report_Type=report)]), STATIONS[0], 2026)
    assert len(frame) == int(accepted)
    assert audit["accepted_exact_hour_rows"] == int(accepted)


@pytest.mark.parametrize("change", [
    {"temperature": ""}, {"temperature": "-9999"}, {"temperature": "99"},
    {"temperature": "inf"}, {"temperature_Source_Station_ID": "ICAO-VOMM"},
    {"temperature_Measurement_Code": "D"}, {"DATE": "2026-01-01T00:30:00"},
])
def test_missing_invalid_derived_wrong_station_and_offhour_values_are_not_targets(change):
    frame, _, _ = parse_ghcnh(encoded([record(**change)]), STATIONS[0], 2026)
    assert frame.empty


def test_wmo_alias_is_explicit_and_does_not_admit_other_sites():
    frame, _, _ = parse_ghcnh(encoded([record(temperature_Source_Station_ID="WMO-42410", temperature_Source_Code="412", temperature_Quality_Code="")]), STATIONS[0], 2026)
    assert len(frame) == 1
    frame, _, _ = parse_ghcnh(encoded([record(temperature_Source_Station_ID="WMO-43279")]), STATIONS[0], 2026)
    assert frame.empty


def test_duplicate_conflicts_are_missing_not_averaged():
    frame, _, audit = parse_ghcnh(encoded([record(), record(temperature="14.3")]), STATIONS[0], 2026)
    assert np.isnan(frame["observed_c"].iloc[0])
    assert audit["duplicate_rows"] == 1
    assert audit["conflicting_hours"] == 1


def test_equal_duplicates_collapse():
    frame, _, audit = parse_ghcnh(encoded([record(), record()]), STATIONS[0], 2026)
    assert len(frame) == 1
    assert audit["conflicting_hours"] == 0


@pytest.mark.parametrize("problem", ["station", "year", "position", "schema"])
def test_structural_errors_fail_closed(problem):
    rows = [record()]
    if problem == "station":
        rows[0]["STATION"] = "other"
    elif problem == "year":
        rows[0]["DATE"] = "2025-12-31T23:00:00"
    elif problem == "position":
        rows.append(record(LATITUDE="27.0"))
    else:
        del rows[0]["temperature_Quality_Code"]
    with pytest.raises(ValueError):
        parse_ghcnh(encoded(rows), STATIONS[0], 2026)


def test_parser_does_not_manufacture_missing_hours():
    rows = [record(), record(DATE="2026-01-01T02:00:00")]
    frame, _, _ = parse_ghcnh(encoded(rows), STATIONS[0], 2026)
    assert len(frame) == 2
    assert pd.Timestamp("2026-01-01T01:00Z") not in frame.index
    assert str(frame.index.tz) == "UTC"


def annual_fetch(changed_position=False):
    def fetch(url, cache_dir):
        station = next(config for config in STATIONS if config["ghcnh_id"] in url)
        year = int(url.rsplit("_", 1)[1].split(".")[0])
        body = encoded([record(
            STATION=station["ghcnh_id"], DATE=f"{year}-01-01T00:00:00",
            temperature_Source_Station_ID=f"ICAO-{station['icao']}",
            ELEVATION="55.0" if changed_position and year == 2025 else "54.0",
        )])
        return body, {"url": url, "sha256": f"test-{station['id']}-{year}"}
    return fetch


def test_annual_loader_keeps_three_sites_and_years_separate(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "fetch", annual_fetch())
    observations, provenance, sources = data.load_years(tmp_path)
    assert len(sources) == 9
    assert len({(source["station"], source["year"]) for source in sources}) == 9
    for station in STATIONS:
        frame = observations[station["id"]]
        assert frame.index.year.tolist() == [2024, 2025, 2026]
        assert frame.index.is_unique
        assert provenance[station["id"]]["station"]["ghcnh_id"] == station["ghcnh_id"]
        assert len(provenance[station["id"]]["sources"]) == 3


@pytest.mark.parametrize("change", ["sha256", "url", "missing"])
def test_annual_loader_rejects_changed_frozen_sources(monkeypatch, tmp_path, change):
    monkeypatch.setattr(data, "fetch", annual_fetch())
    _, _, sources = data.load_years(tmp_path)
    if change == "missing":
        sources.pop(0)
    else:
        sources[0]["source"][change] = "changed"
    with pytest.raises(ValueError, match="frozen snapshot"):
        data.load_years(tmp_path, sources)


def test_annual_loader_rejects_position_changes_between_years(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "fetch", annual_fetch(changed_position=True))
    with pytest.raises(ValueError, match="metadata changed across years"):
        data.load_years(tmp_path)
