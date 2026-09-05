import json

import numpy as np
import pandas as pd
import pytest

from scripts import weather_seasonal as seasonal
from scripts.weather_correction import STATIONS


@pytest.fixture
def protocol():
    return json.loads(seasonal.PROTOCOL.read_text())


def test_frozen_inventory_and_protocol_load():
    protocol, snapshot = seasonal.load_protocol(seasonal.PROTOCOL)
    assert protocol["source_snapshot_sha256"] == seasonal.pilot.sha256(seasonal.ROOT / protocol["source_snapshot"])
    assert len(snapshot["sources"]) == 9


def test_schedule_spans_years_and_seasons_without_reusing_previous_evaluation(protocol):
    scheduled = seasonal.schedule(protocol)
    assert scheduled.groupby("split").size().to_dict() == {"train": 135, "development": 33, "holdout": 99}
    assert scheduled["origin"].dt.hour.eq(6).all()
    assert scheduled["origin"].sub(scheduled["run"]).eq(pd.Timedelta(hours=6)).all()
    assert scheduled.loc[scheduled["split"].eq("train"), "origin_month"].nunique() == 12
    assert set(scheduled.loc[scheduled["split"].eq("holdout"), "calendar_season"]) == {"DJF", "MAM", "JJA"}
    assert not scheduled["origin"].between("2025-04-01", "2025-09-01").any()
    for _, part in scheduled.groupby("station"):
        assert part["origin"].is_unique
        assert part["origin"].diff().dropna().ge(pd.Timedelta(hours=48)).all()


@pytest.mark.parametrize("problem", ["count", "end", "cutoff", "reuse", "old_archive", "future", "overlap", "hour"])
def test_invalid_schedules_are_rejected(protocol, problem):
    if problem == "count":
        protocol["splits"]["train"]["origins_per_station"] = 44
    elif problem == "end":
        protocol["splits"]["train"]["end"] = "2025-03-20T06:00Z"
    elif problem == "cutoff":
        protocol["fit_cutoff"] = "2025-03-21T06:00Z"
    elif problem == "reuse":
        protocol["splits"]["development"] = {"start": "2025-05-01T06:00Z", "end": "2025-05-01T06:00Z", "origins_per_station": 1}
    elif problem == "old_archive":
        protocol["splits"]["train"] = {"start": "2024-03-18T06:00Z", "end": "2024-03-18T06:00Z", "origins_per_station": 1}
    elif problem == "future":
        protocol["splits"]["holdout"] = {"start": "2026-08-31T06:00Z", "end": "2026-08-31T06:00Z", "origins_per_station": 1}
    elif problem == "overlap":
        protocol["splits"]["holdout"] = protocol["splits"]["development"].copy()
    else:
        protocol["splits"]["train"] = {"start": "2024-04-01T00:00Z", "end": "2024-04-01T00:00Z", "origins_per_station": 1}
    with pytest.raises(ValueError):
        seasonal.schedule(protocol)


@pytest.mark.parametrize("problem", ["model", "history", "step", "baseline", "mask", "seasons", "coverage", "seed", "blocks", "replicates", "hash"])
def test_protocol_drift_fails_closed(protocol, tmp_path, problem):
    if problem == "model":
        protocol["timesfm"]["revision"] = "main"
    elif problem == "history":
        protocol["history_policy"]["max_gap_hours"] = 12
    elif problem == "step":
        protocol["origin_step_hours"] = 48
    elif problem == "baseline":
        protocol["scoring"]["primary_baseline"] = "yesterday_c"
    elif problem == "mask":
        protocol["scoring"]["common_observation_mask"] = False
    elif problem == "seasons":
        protocol["scoring"]["calendar_seasons"]["DJF"] = [1, 2]
    elif problem == "coverage":
        protocol["scoring"]["pilot_pass"]["minimum_accepted_origin_fraction"] = 0.5
    elif problem == "seed":
        protocol["scoring"]["bootstrap"]["seed"] = -1
    elif problem == "blocks":
        protocol["scoring"]["bootstrap"]["block_origins"] = 34
    elif problem == "replicates":
        protocol["scoring"]["bootstrap"]["replicates"] = 4000.0
    else:
        protocol["source_snapshot_sha256"] = "changed"
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    with pytest.raises(ValueError):
        seasonal.load_protocol(path)


@pytest.mark.parametrize("problem", ["duplicate", "policy", "identity"])
def test_source_inventory_schema_is_validated_even_with_matching_hash(protocol, tmp_path, problem):
    snapshot = json.loads((seasonal.ROOT / protocol["source_snapshot"]).read_text())
    if problem == "duplicate":
        snapshot["sources"][-1] = snapshot["sources"][0]
    elif problem == "policy":
        snapshot["policy"]["fill_observations"] = True
    else:
        snapshot["stations"][0]["ghcnh_id"] = "wrong"
    source = tmp_path / "sources.json"
    source.write_text(json.dumps(snapshot))
    protocol["source_snapshot"] = str(source)
    protocol["source_snapshot_sha256"] = seasonal.pilot.sha256(source)
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    with pytest.raises(ValueError, match="Invalid source inventory"):
        seasonal.load_protocol(path)


def test_observation_audit_retains_rejections_and_does_not_fill_targets(protocol):
    index = pd.date_range("2024-03-01", "2026-09-01", freq="h", tz="UTC")
    observed = {station: pd.DataFrame({"observed_c": 20.0}, index=index) for station in STATIONS}
    origin = pd.Timestamp(protocol["splits"]["holdout"]["start"])
    observed[STATIONS[0]].loc[origin, "observed_c"] = np.nan
    observed[STATIONS[0]].loc[origin + pd.Timedelta(hours=1), "observed_c"] = np.nan
    observed[STATIONS[1]].loc[origin - pd.Timedelta(hours=7):origin, "observed_c"] = np.nan
    observed[STATIONS[2]].loc[origin + pd.Timedelta(hours=1):origin + pd.Timedelta(hours=48), "observed_c"] = np.nan
    audited = seasonal.audit_origins(protocol, observed)
    assert len(audited) == 267
    first = audited.loc[audited["origin"].eq(origin)].set_index("station")
    assert first.loc[STATIONS[0], "status"] == "eligible"
    assert first.loc[STATIONS[0], "imputed_history_hours"] == 1
    assert first.loc[STATIONS[0], "target_observed_hours"] == 47
    assert first.loc[STATIONS[1], "reason"] == "history_policy"
    assert first.loc[STATIONS[2], "reason"] == "no_verification_observations"


def example():
    origins = pd.to_datetime(["2026-02-28T06:00Z", "2026-05-12T06:00Z", "2026-05-13T06:00Z", "2026-05-21T06:00Z"])
    predictions = pd.DataFrame({"station": STATIONS[0], "origin": origins, "valid_time": origins + pd.Timedelta(hours=24), "observed_c": 20.0, "scored": True})
    for method in seasonal.pilot.METHODS:
        predictions[method] = [21.0, 22.0, 23.0, 24.0]
    observations = {STATIONS[0]: pd.DataFrame({"source_code": "223", "quality_code": "1", "report_type": "FM15", "source_station_id": "ICAO-VEGT"}, index=pd.DatetimeIndex(predictions["valid_time"]))}
    return predictions, observations


def test_annotation_uses_origin_season_run_epoch_and_observation_provenance():
    predictions, observations = example()
    result = seasonal.annotate(predictions, observations)
    assert result["calendar_season"].tolist() == ["DJF", "MAM", "MAM", "MAM"]
    assert result["origin_month"].iloc[0] == "2026-02"  # Its target is in March.
    assert result["forecast_epoch"].tolist() == ["ifs_49r1_hindcast"] * 2 + ["ifs_50r1_archive_availability_unverified"] * 2
    assert result["guidance_crosses_ifs_update"].tolist() == [False, False, True, False]
    assert seasonal.forecast_epoch(seasonal.EPOCH_BOUNDARY).startswith("ifs_50r1")
    assert result["verification_report_type"].eq("FM15").all()
    pd.testing.assert_frame_equal(predictions, result[predictions.columns])


def test_seasonal_tables_use_common_mask_and_keep_rejections_in_denominator():
    predictions, observations = example()
    predictions.loc[2, ["observed_c", "scored"]] = [np.nan, False]
    annotated = seasonal.annotate(predictions, observations)
    ledger = annotated[["station", "origin", "calendar_season"]].copy()
    ledger["status"], ledger["scored_hours"] = "scored", [1, 1, 0, 1]
    ledger.loc[2, "status"] = "data_rejected"
    tables = seasonal.seasonal_tables(ledger, annotated)
    mam = tables["per_calendar_season.csv"].query("calendar_season == 'MAM'")
    assert mam["n"].eq(2).all()
    assert np.allclose(mam["rmse_c"], np.sqrt((4 + 16) / 2))
    coverage = tables["season_coverage.csv"].set_index("calendar_season").loc["MAM"]
    assert coverage["planned_cases"] == 3
    assert coverage["accepted_cases"] == 2
    assert coverage["data_rejected_cases"] == 1
    assert coverage["planned_hours"] == 144


@pytest.mark.parametrize("enough", [True, False])
def test_seasonal_decision_counts_dates_not_station_rows(protocol, monkeypatch, enough):
    monkeypatch.setattr(seasonal.pilot, "pilot_decision", lambda *args: {"checks": {"other": True}, "retrospective_pilot_pass": True})
    ledger = seasonal.schedule(protocol).query("split == 'holdout'").copy()
    ledger["status"] = "scored"
    if not enough:
        dates = sorted(ledger.loc[ledger["calendar_season"].eq("DJF"), "origin"].unique())
        ledger.loc[ledger["origin"].isin(dates[5:]), "status"] = "data_rejected"
    decision = seasonal.seasonal_decision({}, {}, ledger, protocol)
    assert decision["retrospective_pilot_pass"] is enough
    if not enough:
        assert decision["accepted_origin_dates_by_season"]["DJF"] == 5
