"""Export a browser-readable view of the frozen holdout, without running inference."""

import argparse
import csv
import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "results/weather/india_station_seasonal_v1/holdout"
STATIONS = [
    {"id": "42410099999", "name": "Guwahati", "region": "Assam", "icao": "VEGT", "latitude": 26.1, "longitude": 91.58},
    {"id": "43128599999", "name": "Hyderabad", "region": "Telangana", "icao": "VOHS", "latitude": 17.2333, "longitude": 78.4167},
    {"id": "43279099999", "name": "Chennai", "region": "Tamil Nadu", "icao": "VOMM", "latitude": 12.9944, "longitude": 80.1805},
]
METHODS = {"timesfm_nwp_c": "forecast", "ecmwf_ifs_c": "ecmwf", "ridge_corrected_c": "ridge"}


def number(value):
    if value is None or value == "":
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite temperature in saved artifact")
    return result


def read_csv(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def iso(value):
    return datetime.fromisoformat(value).isoformat()


def case_document(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    path = directory / "predictions.csv"
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["artifact_sha256"]["predictions.csv"]:
        raise ValueError(f"Artifact checksum mismatch: {directory.name}")
    rows = sorted(read_csv(path), key=lambda row: row["valid_time"])
    origin = datetime.fromisoformat(manifest["origin"])
    if len(rows) != 48 or any(datetime.fromisoformat(row["valid_time"]) != origin + timedelta(hours=i) for i, row in enumerate(rows, 1)):
        raise ValueError(f"Expected 48 aligned forecast hours: {directory.name}")
    points = []
    for row in rows:
        values = {key: number(row[column]) for column, key in METHODS.items()}
        lower, upper = number(row["timesfm_nwp_p10_c"]), number(row["timesfm_nwp_p90_c"])
        if any(value is None for value in values.values()) or not lower <= values["forecast"] <= upper:
            raise ValueError("Incomplete forecast or invalid quantile range")
        points.append({
            "time": iso(row["valid_time"]), **values,
            "observed": number(row["observed_c"]) if row["scored"] == "True" else None,
            "lower": lower, "upper": upper,
        })
    return {
        "id": directory.name, "stationId": manifest["station"], "mode": "historical_replay",
        "origin": manifest["origin"], "guidanceRun": manifest["run"],
        "quantilesCalibrated": False, "points": points,
        "provenance": {"model": manifest["timesfm_model"]["model_id"], "revision": manifest["timesfm_model"]["revision"],
                       "device": manifest["timesfm_model"]["device"], "sha256": manifest["artifact_sha256"]["predictions.csv"]},
    }


def export(output, source=SOURCE):
    manifest = json.loads((source / "manifest.json").read_text())
    for name in ("origins.csv", "per_station.csv", "per_lead.csv"):
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != manifest["artifact_sha256"][name]:
            raise ValueError(f"Benchmark artifact checksum mismatch: {name}")
    documents = []
    for directory in sorted((source / "cases").iterdir()):
        if json.loads((directory / "manifest.json").read_text()).get("status") == "scored":
            documents.append(case_document(directory))
    if len(documents) != manifest["results"]["counts"]["accepted_origins"]:
        raise ValueError("Case count does not match holdout manifest")
    scores = [{"method": METHODS[key], **value} for key, value in manifest["results"]["scores"].items() if key in METHODS]
    station_scores = [
        {"stationId": row["station"], "method": METHODS[row["method"]],
         **{key: number(row[key]) for key in ("n", "mae_c", "rmse_c", "bias_c")}}
        for row in read_csv(source / "per_station.csv") if row["method"] in METHODS
    ]
    lead_scores = [
        {"lead": int(row["lead_hours"]), "method": METHODS[row["method"]],
         **{key: number(row[key]) for key in ("n", "mae_c", "rmse_c")}}
        for row in read_csv(source / "per_lead.csv") if row["method"] in METHODS
    ]
    catalog = {
        "stations": STATIONS,
        "cases": [{key: document[key] for key in ("id", "stationId", "origin", "guidanceRun")} for document in documents],
        "benchmark": {"scores": scores, "stationScores": station_scores, "leadScores": lead_scores,
                      "counts": manifest["results"]["counts"], "comparison": manifest["primary_comparison"],
                      "limits": manifest["limits"], "completedAt": manifest["finished_at"],
                      "protocolSha256": manifest["protocol_sha256"]},
        "runs": read_csv(source / "origins.csv"),
    }
    (output / "cases").mkdir(parents=True, exist_ok=True)
    for name, data in [("catalog.json", catalog), *[(f"cases/{doc['id']}.json", doc) for doc in documents]]:
        (output / name).write_text(json.dumps(data, allow_nan=False, separators=(",", ":")) + "\n")
    return catalog


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "frontend/public/data")
    args = parser.parse_args()
    catalog = export(args.output)
    print(f"Exported {len(catalog['cases'])} verified historical cases; no inference or live requests.")
