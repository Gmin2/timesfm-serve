"""Delayed observation scoring and frozen, chronological training datasets."""

import hashlib
import json
from collections import Counter
from datetime import timedelta

import numpy as np

from timesfm_serve.weather_live_policy import STATIONS, input_digest, parse_utc

HORIZON = timedelta(hours=48)


def digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, allow_nan=False).encode()).hexdigest()


def metrics(observed, predicted):
    observed, predicted = np.asarray(observed, dtype=float), np.asarray(predicted, dtype=float)
    mask = np.isfinite(observed) & np.isfinite(predicted)
    error = predicted[mask] - observed[mask]
    return {"hours": len(error), "mae_c": float(np.abs(error).mean()) if len(error) else None,
            "rmse_c": float(np.sqrt(np.square(error).mean())) if len(error) else None,
            "bias_c": float(error.mean()) if len(error) else None}


def examples_from_snapshots(rows, as_of):
    """Use actual, unfilled observations from later accepted source snapshots."""
    from timesfm_serve.weather_ingest import reconstruct

    as_of = parse_utc(as_of)
    labels, first_seen, label_sources, inputs = {}, {}, {}, []
    for row in rows:
        document = row["inputs"]
        if input_digest(document) != row["snapshot_sha256"]:
            raise ValueError("Stored live input checksum mismatch")
        if parse_utc(document["captured_at"]) > as_of:
            continue
        history, guidance = reconstruct(document)
        station = document["station_id"]
        for timestamp, value in history["observed_c"].dropna().items():
            if timestamp.to_pydatetime() <= as_of - timedelta(hours=1):
                key = station, timestamp.to_pydatetime()
                labels.setdefault(key, set()).add(float(value))
                captured = parse_utc(document["captured_at"])
                first_seen[key] = min(first_seen.get(key, captured), captured)
                label_sources.setdefault(key, set()).add(row["snapshot_sha256"])
        inputs.append((row, history, guidance))
    examples = []
    for row, history, guidance in inputs:
        if row.get("forecast") is None or row.get("published_at") is None:
            continue
        document, forecast = row["inputs"], row["forecast"]
        origin, published = parse_utc(document["forecast_origin"]), parse_utc(row["published_at"])
        points = forecast["points"]
        if len(points) != 48:
            raise ValueError("Expected a complete 48-hour forecast")
        targets, available, sources = [], [], set()
        for lead, point in enumerate(points, 1):
            timestamp = origin + timedelta(hours=lead)
            if parse_utc(point["valid_time"]) != timestamp:
                raise ValueError("Forecast timestamps are not contiguous")
            key = document["station_id"], timestamp
            values = labels.get(key, set())
            # Exclude both conflicting reports and hours already past at publication.
            targets.append(next(iter(values)) if len(values) == 1 and published < timestamp else None)
            available.append(first_seen[key].isoformat() if targets[-1] is not None else None)
            sources.update(label_sources.get(key, set()))
        examples.append({
            "id": str(row["job_id"]), "station_id": document["station_id"], "origin": origin.isoformat(),
            "source_sha256": row["snapshot_sha256"], "published_at": published.isoformat(),
            "context": history["model_input_c"].tolist(),
            "observed_context": [None if np.isnan(v) else float(v) for v in history["observed_c"]],
            "covariate": guidance["nwp_c"].tolist(), "targets": targets,
            "target_available_at": available, "label_snapshot_sha256": sorted(sources),
            "current": [point["temperature_2m"] for point in points],
            "ecmwf": guidance["nwp_c"].iloc[-48:].tolist(),
        })
    return examples


def dataset(examples, as_of, minimum_train=8, minimum_validation=3):
    """48-hour spacing, a seven-day validation block, and purged target boundaries."""
    as_of = parse_utc(as_of)
    ready = [e for e in examples if parse_utc(e["origin"]) + HORIZON <= as_of - timedelta(hours=1)
             and sum(v is not None for v in e["targets"]) >= 43]
    if not ready:
        raise ValueError("not_enough_mature_observations")
    latest = max(parse_utc(e["origin"]) for e in ready)
    cutoff = latest - timedelta(days=7)
    selected, last = [], {}
    for example in sorted(ready, key=lambda e: (e["origin"], e["station_id"])):
        origin, station = parse_utc(example["origin"]), example["station_id"]
        if station in last and origin < last[station] + HORIZON:
            continue
        if origin + HORIZON < cutoff:
            split = "train"
            targets = [value if available is not None and parse_utc(available) <= cutoff else None
                       for value, available in zip(example["targets"], example["target_available_at"], strict=True)]
            if sum(v is not None for v in targets) < 43:
                continue
            example = example | {"targets": targets}
        elif origin >= cutoff:
            split = "validation"
        else:
            continue
        selected.append(example | {"split": split})
        last[station] = origin
    counts = Counter((e["split"], e["station_id"]) for e in selected)
    if any(counts[split, station] < minimum for split, minimum in
           (("train", minimum_train), ("validation", minimum_validation)) for station in STATIONS):
        raise ValueError("not_enough_independent_station_windows")
    result = {"schema_version": 1, "purpose": "research_only", "as_of": as_of.isoformat(),
              "validation_start": cutoff.isoformat(), "observation_policy": "awc_metar_structural_checks_v1",
              "examples": selected}
    validate_dataset(result)
    return result


def validate_dataset(document):
    if document.get("schema_version") != 1 or document.get("purpose") != "research_only":
        raise ValueError("Only versioned research datasets are supported")
    cutoff, as_of = parse_utc(document["validation_start"]), parse_utc(document["as_of"])
    if not 2 <= len(document["examples"]) <= 1024:
        raise ValueError("Training datasets must contain 2 to 1024 windows")
    previous, splits, identities = {}, set(), set()
    for example in sorted(document["examples"], key=lambda e: (e["origin"], e["station_id"])):
        station, origin, split = example["station_id"], parse_utc(example["origin"]), example["split"]
        if station not in STATIONS or split not in ("train", "validation"):
            raise ValueError("Unknown station or split")
        if example["id"] in identities:
            raise ValueError("Duplicate training example")
        identities.add(example["id"])
        if origin + HORIZON > as_of - timedelta(hours=1):
            raise ValueError("Training labels have not matured")
        if (split == "train" and origin + HORIZON >= cutoff) or (split == "validation" and origin < cutoff):
            raise ValueError("Training and validation target windows overlap")
        if station in previous and origin < previous[station] + HORIZON:
            raise ValueError("Overlapping forecast targets")
        for name, size in (("context", 168), ("observed_context", 168), ("covariate", 216),
                           ("targets", 48), ("current", 48), ("ecmwf", 48)):
            values = np.asarray(example[name], dtype=float)
            if values.shape != (size,) or np.isinf(values).any():
                raise ValueError(f"Invalid {name} shape or values")
            if name not in ("observed_context", "targets") and not np.isfinite(values).all():
                raise ValueError(f"Missing {name}")
        if np.isfinite(np.asarray(example["targets"], dtype=float)).sum() < 43:
            raise ValueError("Insufficient observed targets")
        if not document.get("engineering_smoke"):
            if len(example.get("target_available_at", [])) != 48:
                raise ValueError("Live labels need auditable availability times")
            for lead, (value, available) in enumerate(zip(example["targets"], example["target_available_at"], strict=True), 1):
                if value is not None and (available is None or not origin + timedelta(hours=lead) <= parse_utc(available) <= (cutoff if split == "train" else as_of)):
                    raise ValueError("Observation was not available at the training cutoff")
        if not np.array_equal(example["covariate"][-48:], example["ecmwf"]):
            raise ValueError("ECMWF comparison must use the same guidance")
        previous[station] = origin
        splits.add(split)
    if splits != {"train", "validation"}:
        raise ValueError("Both chronological splits are required")


def load_live_examples(as_of):
    from psycopg.rows import dict_row

    from timesfm_serve import db

    with db.conn() as connection, connection.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(
            "select i.document as inputs, i.sha256 as snapshot_sha256, j.id as job_id,"
            " f.document as forecast, f.published_at from weather_live_inputs i"
            " join weather_jobs j on j.input_snapshot_id = i.id"
            " left join weather_forecasts f on f.job_id = j.id"
            " where i.captured_at between %s - interval '90 days' and %s order by i.forecast_origin",
            (as_of, as_of),
        ).fetchall()
    return examples_from_snapshots(rows, as_of)


def refresh_scores(examples, as_of):
    from psycopg.types.json import Jsonb

    from timesfm_serve import db

    reports = []
    with db.conn() as connection, connection.transaction():
        for example in examples:
            report = {name: metrics(example["targets"], example[name]) for name in ("current", "ecmwf")}
            connection.execute(
                "insert into weather_evaluations (job_id, checked_at, report) values (%s,%s,%s)"
                " on conflict (job_id) do update set checked_at=excluded.checked_at, report=excluded.report",
                (example["id"], as_of, Jsonb(report)),
            )
            reports.append(report)
    return {"forecasts": len(reports), "matched_forecast_hours": sum(r["current"]["hours"] for r in reports)}
