import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts import weather_correction_backtest as backtest
from scripts.weather_correction import STATIONS


@pytest.fixture
def protocol():
    return json.loads(backtest.PROTOCOL.read_text())


def test_frozen_schedule_is_chronological_and_excludes_old_test(protocol):
    schedule = backtest.schedule(protocol)
    assert len(schedule) == 177
    assert schedule.groupby("split").size().to_dict() == {"train": 90, "development": 42, "holdout": 45}
    for _, group in schedule.groupby("station"):
        assert group["origin"].diff().dropna().ge(pd.Timedelta(hours=48)).all()
        assert group["origin"].max() < pd.Timestamp("2025-06-01T00:00Z")


@pytest.mark.parametrize("change", ["history", "model", "cutoff", "overlap", "selection"])
def test_protocol_drift_is_rejected(protocol, tmp_path, change):
    if change == "history":
        protocol["history_policy"]["context_hours"] = 672
    elif change == "model":
        protocol["timesfm"]["revision"] = "main"
    elif change == "cutoff":
        protocol["fit_cutoff"] = "2025-02-01T06:00Z"
    elif change == "overlap":
        protocol["splits"]["development"] = protocol["splits"]["train"]
    else:
        protocol["selection"]["split"] = "holdout"
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    with pytest.raises(ValueError):
        backtest.load_protocol(path)


def test_audit_retains_all_origins_and_accepts_a_filled_previous_day(protocol):
    index = pd.date_range("2025-01-01", "2025-06-01", freq="h", tz="UTC")
    observations = {station: pd.Series(20.0, index=index) for station in STATIONS}
    first_origin = pd.Timestamp(protocol["splits"]["train"]["start"])
    observations[STATIONS[0]].loc[first_origin] = np.nan
    observations[STATIONS[0]].loc[first_origin + pd.Timedelta(hours=1)] = np.nan
    observations[STATIONS[1]].loc[first_origin - pd.Timedelta(hours=7):first_origin] = np.nan
    audit = backtest.audit_origins(protocol, observations)
    assert len(audit) == 177
    first = audit.loc[audit["origin"].eq(first_origin)].set_index("station")
    assert first.loc[STATIONS[0], "status"] == "eligible"
    assert first.loc[STATIONS[0], "imputed_history_hours"] == 1
    assert first.loc[STATIONS[0], "target_observed_hours"] == 47
    assert first.loc[STATIONS[1], "status"] == "data_rejected"


def scored_example():
    frames, ledger = [], []
    for i, origin in enumerate(pd.date_range("2025-05-01T06:00Z", periods=4, freq="48h")):
        for station in STATIONS:
            frame = pd.DataFrame({"origin": origin, "station": station, "valid_time": pd.date_range(origin + pd.Timedelta(hours=1), periods=48, freq="h"), "lead_hours": np.arange(1, 49)})
            frame["observed_c"] = 0.0
            frame["scored"] = True
            for method in backtest.METHODS:
                frame[method] = float(i + 1)
            frame["bias_corrected_c"] = 0.0
            for prefix in ["timesfm_", "timesfm_nwp_"]:
                for q in range(10, 100, 10):
                    frame[f"{prefix}p{q}_c"] = (q - 50) / 10 + i + 1
            frames.append(frame)
            ledger.append({"station": station, "origin": origin, "status": "scored", "target_observed_hours": 48, "reason": ""})
    return pd.DataFrame(ledger), pd.concat(frames, ignore_index=True)


def test_metrics_share_masks_and_pool_squared_errors(protocol):
    ledger, predictions = scored_example()
    summary, tables = backtest.summarize(ledger, predictions, protocol)
    expected = np.sqrt(np.mean([1, 4, 9, 16]))
    assert summary["scores"]["ecmwf_ifs_c"]["rmse_c"] == pytest.approx(expected)
    assert summary["counts"]["scored_hours"] == 12 * 48
    assert len(tables["per_lead.csv"]) == 48 * len(backtest.METHODS)
    assert backtest.select_candidate(summary) == "bias_corrected_c"
    predictions.loc[0, "observed_c"] = np.nan
    with pytest.raises(ValueError, match="verification mask"):
        backtest.summarize(ledger, predictions, protocol)


def test_rejections_stay_in_coverage_denominators(protocol):
    ledger, predictions = scored_example()
    extra = ledger.iloc[:1].copy()
    extra["status"], extra["reason"] = "data_rejected", "history_policy"
    summary, _ = backtest.summarize(pd.concat([ledger, extra]), predictions, protocol)
    assert summary["counts"]["accepted_origin_fraction"] == 12 / 13
    assert summary["counts"]["scored_hour_fraction"] == 12 / 13
    assert summary["rejection_reasons"] == {"history_policy": 1}


def test_date_block_bootstrap_is_paired_deterministic_and_row_order_invariant(protocol):
    ledger, predictions = scored_example()
    config = protocol["scoring"]["bootstrap"]
    first = backtest.paired_bootstrap(predictions, ledger["origin"], "bias_corrected_c", config)
    second = backtest.paired_bootstrap(predictions.sample(frac=1, random_state=1), ledger["origin"], "bias_corrected_c", config)
    assert first == second
    assert first["paired_hours"] == 12 * 48
    assert first["scheduled_origin_dates"] == 4
    assert first["rmse_skill_percent"] == 100
    assert first["rmse_delta_95ci_c"][1] < 0


def test_pilot_pass_does_not_claim_operational_or_indus_superiority(protocol):
    ledger, predictions = scored_example()
    summary, _ = backtest.summarize(ledger, predictions, protocol)
    comparison = backtest.paired_bootstrap(predictions, ledger["origin"], "bias_corrected_c", protocol["scoring"]["bootstrap"])
    decision = backtest.pilot_decision(summary, comparison, protocol)
    assert not decision["retrospective_pilot_pass"]  # Only four origins per station.
    assert not decision["operational_ecmwf_superiority_established"]
    assert not decision["india_wide_or_indus_superiority_established"]


@pytest.mark.parametrize("problem", ["missing", "incomplete", "code", "artifact", "inventory"])
def test_stage_gate_rejects_unverified_predecessor(tmp_path, problem):
    identity = {"protocol_sha256": "p", "pipeline_sha256": {"file": "code"}}
    output = tmp_path / "development"
    output.mkdir()
    payload = output / "predictions.csv"
    payload.write_text("test\n")
    manifest = identity | {"run_status": "complete", "artifact_sha256": backtest.artifact_hashes(output)}
    if problem == "incomplete":
        manifest["run_status"] = "incomplete"
    elif problem == "code":
        manifest["pipeline_sha256"] = {}
    elif problem == "artifact":
        payload.write_text("changed\n")
    elif problem == "inventory":
        manifest["artifact_sha256"] = {}
    if problem != "missing":
        backtest.write_json(output / "manifest.json", manifest)
    with pytest.raises(ValueError):
        backtest.require_stage(tmp_path, "development", identity)


def test_stage_gate_accepts_complete_verified_artifacts(tmp_path):
    identity = {"protocol_sha256": "p"}
    output = tmp_path / "train"
    output.mkdir()
    (output / "model.json").write_text("{}")
    backtest.write_json(output / "manifest.json", identity | {"run_status": "complete", "artifact_sha256": backtest.artifact_hashes(output)})
    assert backtest.require_stage(tmp_path, "train", identity)["run_status"] == "complete"


def collection_inputs(tmp_path):
    origin = pd.Timestamp("2025-02-04T06:00Z")
    index = pd.date_range(origin - pd.Timedelta(hours=167), periods=216, freq="h")
    observed = pd.Series(20.0, index=index)
    ledger = pd.DataFrame([{"station": STATIONS[0], "split": "train", "origin": origin, "run": origin - pd.Timedelta(hours=6), "status": "eligible", "reason": "", "detail": "", "scored_hours": 0}])
    args = SimpleNamespace(cache_dir=tmp_path, device="mps", model_cache_dir=tmp_path, offline_model=True)
    provenance = {STATIONS[0]: {"station": {"id": STATIONS[0]}}}
    return observed, ledger, args, provenance


def test_pre_rejected_cases_never_fetch_or_run_models(tmp_path, monkeypatch):
    observed, ledger, args, provenance = collection_inputs(tmp_path)
    ledger["status"] = "data_rejected"
    monkeypatch.setattr(backtest, "load_forecast", lambda *args: pytest.fail("Must not fetch"))
    result, frame = backtest.collect({}, ledger, {STATIONS[0]: observed}, provenance, tmp_path, {}, args)
    assert result["status"].eq("data_rejected").all()
    assert frame.empty


@pytest.mark.parametrize("failure,expected", [("network", "forecast_error"), ("missing", "data_rejected")])
def test_runtime_failures_are_not_silent_data_exclusions(tmp_path, monkeypatch, failure, expected):
    observed, ledger, args, provenance = collection_inputs(tmp_path)

    def forecast(*args):
        if failure == "network":
            raise RuntimeError("network failed")
        return observed * np.nan, {}, {}

    monkeypatch.setattr(backtest, "load_forecast", forecast)
    result, frame = backtest.collect({}, ledger, {STATIONS[0]: observed}, provenance, tmp_path, {}, args)
    assert result["status"].iloc[0] == expected
    assert frame.empty


def test_verified_case_resume_does_not_fetch_again(tmp_path, monkeypatch):
    observed, ledger, args, provenance = collection_inputs(tmp_path)
    monkeypatch.setattr(backtest, "load_forecast", lambda *args: (observed, {}, {}))
    first, first_frame = backtest.collect({}, ledger, {STATIONS[0]: observed}, provenance, tmp_path, {}, args)
    monkeypatch.setattr(backtest, "load_forecast", lambda *args: pytest.fail("Verified case should resume"))
    second, second_frame = backtest.collect({}, ledger, {STATIONS[0]: observed}, provenance, tmp_path, {}, args)
    assert first["status"].iloc[0] == second["status"].iloc[0] == "ready"
    pd.testing.assert_frame_equal(first_frame, second_frame, check_dtype=False)
    saved = next((tmp_path / "cases").glob("*/predictions.csv"))
    saved.write_text("corrupt\n")
    with pytest.raises(ValueError, match="Artifact verification"):
        backtest.collect({}, ledger, {STATIONS[0]: observed}, provenance, tmp_path, {}, args)
