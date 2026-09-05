"""Audited multi-year GHCNh temperature input, separate from the frozen ISD pilots."""

import argparse
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.weather_case import CACHE, ROOT, fetch

BASE = "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly"
STATIONS = [
    {"id": "42410099999", "ghcnh_id": "INU042410-1", "icao": "VEGT", "wmo": "42410", "name": "Guwahati"},
    {"id": "43128599999", "ghcnh_id": "INI0000VOHS", "icao": "VOHS", "wmo": None, "name": "Hyderabad"},
    {"id": "43279099999", "ghcnh_id": "INI0000VOMM", "icao": "VOMM", "wmo": "43279", "name": "Chennai"},
]
POLICY = {
    "version": "ghcnh_direct_temperature_v1",
    "units": "degrees_celsius_already_scaled",
    "exact_hours_only": True,
    "source_223": {"quality_codes": ["1"], "report_types": ["FM12", "FM15"]},
    "source_412": {"quality_codes": [""], "report_types": ["FM12", "FM94_1"]},
    "source_413": {"quality_codes": [""], "report_types": ["FM15"]},
    "blank_quality_interpretation": "No supplied flag, not proof that all quality checks passed.",
    "empty_measurement_code_required": True,
    "conflicting_duplicates": "missing",
    "fill_observations": False,
}
COLUMNS = [
    "STATION", "Station_name", "DATE", "LATITUDE", "LONGITUDE", "ELEVATION", "temperature",
    "temperature_Measurement_Code", "temperature_Quality_Code", "temperature_Report_Type",
    "temperature_Source_Code", "temperature_Source_Station_ID",
]


def parse_ghcnh(body, config, year):
    frame = pd.read_csv(io.BytesIO(body), sep="|", usecols=COLUMNS, dtype=str, keep_default_na=False)
    if frame.empty or set(frame["STATION"]) != {config["ghcnh_id"]}:
        raise ValueError("Expected exactly the requested GHCNh station")
    positions = frame[["LATITUDE", "LONGITUDE", "ELEVATION"]].drop_duplicates().astype(float)
    if len(positions) != 1 or not np.isfinite(positions.to_numpy()).all():
        raise ValueError("GHCNh station position is missing or changes within the file")
    latitude, longitude, elevation = positions.iloc[0]
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and -500 <= elevation <= 9000):
        raise ValueError("Invalid station coordinates or elevation")
    dates = pd.to_datetime(frame["DATE"], utc=True, errors="raise")
    if dates.isna().any() or not dates.dt.year.eq(year).all():
        raise ValueError("GHCNh timestamps must belong to the requested year")
    temperature = pd.to_numeric(frame["temperature"].replace("", np.nan), errors="raise")
    source = frame["temperature_Source_Code"].str.strip()
    quality = frame["temperature_Quality_Code"].str.strip()
    report = frame["temperature_Report_Type"].str.strip()
    source_id = frame["temperature_Source_Station_ID"].str.strip()
    allowed_ids = {f"ICAO-{config['icao']}"}
    if config["wmo"]:
        allowed_ids.add(f"WMO-{config['wmo']}")
    # GHCNh harmonizes each variable: a SYNOP temperature can replace a METAR
    # at the same timestamp. Keep both direct report types, with source-specific QC.
    source_quality = (
        (source.eq("223") & quality.eq("1") & report.isin(["FM12", "FM15"]))
        | (source.eq("412") & quality.eq("") & report.isin(["FM12", "FM94_1"]))
        | (source.eq("413") & quality.eq("") & report.eq("FM15"))
    )
    finite = np.isfinite(temperature) & temperature.between(-93.2, 61.8)
    identity = source_id.isin(allowed_ids)
    measured = frame["temperature_Measurement_Code"].str.strip().eq("")
    exact_hour = dates.eq(dates.dt.floor("h"))
    accepted = finite & source_quality & identity & measured & exact_hour
    records = pd.DataFrame({
        "valid_time": dates[accepted], "observed_c": temperature[accepted],
        "source_code": source[accepted], "quality_code": quality[accepted],
        "report_type": report[accepted], "source_station_id": source_id[accepted],
    })
    distinct = records.groupby("valid_time")["observed_c"].nunique()
    hourly = records.groupby("valid_time").first().sort_index()
    hourly.loc[distinct.gt(1), "observed_c"] = np.nan
    metadata = {
        "id": config["id"], "ghcnh_id": config["ghcnh_id"], "name": config["name"],
        "latitude": float(latitude), "longitude": float(longitude), "elevation_m": float(elevation),
    }
    quality_counts = frame.groupby(["temperature_Source_Code", "temperature_Quality_Code", "temperature_Report_Type"]).size().rename("rows").reset_index().to_dict("records")
    audit = {
        "raw_rows": len(frame), "accepted_exact_hour_rows": int(accepted.sum()),
        "accepted_unique_hours": int(hourly["observed_c"].notna().sum()),
        "off_hour_rows": int((~exact_hour).sum()), "invalid_temperature_rows": int((~finite).sum()),
        "rejected_source_quality_report_rows": int((~source_quality).sum()),
        "rejected_identity_rows": int((~identity).sum()), "rejected_measurement_rows": int((~measured).sum()),
        "duplicate_rows": len(records) - len(hourly), "conflicting_hours": int(distinct.gt(1).sum()),
        "first_report": dates.min().isoformat(), "last_report": dates.max().isoformat(),
        "source_quality_report_counts": quality_counts,
    }
    return hourly, metadata, audit


def load_years(cache_dir=CACHE, expected=None):
    observations, provenance, sources = {}, {}, []
    expected_by_key = {(row["station"], row["year"]): row for row in expected} if expected is not None else None
    for config in STATIONS:
        pieces, metadata = [], None
        for year in [2024, 2025, 2026]:
            url = f"{BASE}/access/by-year/{year}/psv/GHCNh_{config['ghcnh_id']}_{year}.psv"
            body, source = fetch(url, cache_dir)
            if expected_by_key is not None:
                saved = expected_by_key.get((config["id"], year))
                if saved is None or saved["source"]["sha256"] != source["sha256"] or saved["source"]["url"] != source["url"]:
                    raise ValueError("GHCNh source differs from the frozen snapshot")
            hourly, position, audit = parse_ghcnh(body, config, year)
            if metadata is not None and position != metadata:
                raise ValueError("Station metadata changed across years; inspect before merging")
            metadata = position
            pieces.append(hourly)
            sources.append({"station": config["id"], "year": year, "source": source, "audit": audit, "metadata": metadata})
        combined = pd.concat(pieces).sort_index()
        if not combined.index.is_unique:
            raise ValueError("Annual observation files overlap")
        observations[config["id"]] = combined
        provenance[config["id"]] = {"station": metadata, "sources": [row for row in sources if row["station"] == config["id"]]}
    return observations, provenance, sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=CACHE)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/weather/seasonal_data_audit_v1")
    args = parser.parse_args()
    observations, provenance, sources = load_years(args.cache_dir)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    snapshot = {"schema_version": 1, "policy": POLICY, "stations": STATIONS, "sources": sources}
    path = output / "sources.json"
    body = json.dumps(snapshot, indent=2, allow_nan=False) + "\n"
    if path.exists() and path.read_text() != body:
        raise ValueError("Do not overwrite a different data snapshot; use a new output directory")
    path.write_text(body)
    rows = []
    for station, frame in observations.items():
        for month, part in frame.groupby(frame.index.strftime("%Y-%m")):
            rows.append({"station": station, "month": month, "observed_hours": int(part["observed_c"].notna().sum()), "report_types": ";".join(sorted(set(part["report_type"])))})
    pd.DataFrame(rows).to_csv(output / "monthly_coverage.csv", index=False)
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"Source snapshot SHA-256: {hashlib.sha256(path.read_bytes()).hexdigest()}")
    print(f"Data audit only; no forecast scores. Artifacts: {output}")


if __name__ == "__main__":
    main()
