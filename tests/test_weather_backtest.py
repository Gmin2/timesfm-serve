import copy
import json

import numpy as np
import pandas as pd
import pytest

from scripts import weather_backtest as bt
from scripts.weather_backtest import PROTOCOL, audit_origins, load_protocol, require_development, schedule, summarize
from scripts.weather_model import QUANTILE_COLUMNS


def protocol():
    return load_protocol(PROTOCOL)[0]


def test_frozen_schedule_has_disjoint_chronological_windows():
    config = protocol()
    planned = schedule(config)
    assert planned.groupby("split").size().to_dict() == {"development": 14, "holdout": 25}
    assert planned["origin"].diff().dropna().ge(pd.Timedelta(hours=48)).all()
    assert planned["origin"].sub(planned["run"]).eq(pd.Timedelta(hours=6)).all()
    assert pd.Timestamp("2025-07-01T06:00Z") not in planned["origin"].tolist()


def test_protocol_rejects_silent_model_or_policy_changes(tmp_path):
    config = protocol()
    config["history_policy"]["max_gap_hours"] = 24
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="history_policy"):
        load_protocol(path)


def test_previously_seen_forecasts_cannot_enter_the_evaluation():
    config = protocol()
    config["splits"]["holdout"] = {"start": "2025-07-01T06:00Z", "end": "2025-07-01T06:00Z", "origins": 1}
    with pytest.raises(ValueError, match="previously inspected"):
        schedule(config)


def test_overlapping_split_targets_are_rejected():
    config = protocol()
    config["splits"]["holdout"] = copy.deepcopy(config["splits"]["development"])
    with pytest.raises(ValueError, match="chronological"):
        schedule(config)


def test_audit_retains_rejections_and_separates_target_from_input_gaps():
    config = protocol()
    config["splits"] = {
        "development": {"start": "2025-06-01T06:00Z", "end": "2025-06-03T06:00Z", "origins": 2},
        "holdout": {"start": "2025-07-05T06:00Z", "end": "2025-07-05T06:00Z", "origins": 1},
    }
    index = pd.date_range("2025-05-01", "2025-07-08", freq="h", tz="UTC")
    observed = pd.Series(25.0, index=index)
    observed.loc["2025-06-01T05:00Z"] = np.nan
    observed.loc["2025-06-03T10:00Z"] = np.nan
    observed.loc["2025-07-05T07:00Z":"2025-07-07T06:00Z"] = np.nan
    audit = audit_origins(observed, config)
    assert len(audit) == 3
    assert audit.iloc[0]["status"] == "data_rejected"
    assert audit.iloc[0]["reason"] == "incomplete_previous_day"
    assert audit.iloc[1]["status"] == "eligible"
    assert audit.iloc[1]["target_observed_hours"] == 47
    assert audit.iloc[2]["reason"] == "no_verification_observations"


def test_aggregation_pools_errors_and_reports_denominators():
    ledger = pd.DataFrame({
        "status": ["scored", "scored", "data_rejected"],
        "reason": ["", "", "history_policy"],
        "target_observed_hours": [1, 3, 48],
    })
    point = np.array([0.0, 4.0, 4.0, 4.0])
    predictions = pd.DataFrame({
        "origin": ["a", "b", "b", "b"],
        "lead_hours": [1, 1, 2, 3],
        "observed_c": 0.0,
        "persistence_c": 1.0,
        "yesterday_c": 1.0,
        "ecmwf_ifs_c": 1.0,
        "timesfm_c": point,
        "scored": True,
    })
    predictions[QUANTILE_COLUMNS] = point[:, None] + np.arange(-4.0, 5.0)
    summary, tables = summarize(ledger, predictions, protocol())
    assert summary["scores"]["timesfm_c"]["rmse_c"] == pytest.approx(12**0.5)
    assert summary["scores"]["timesfm_c"]["mae_c"] == 3.0
    assert summary["counts"]["accepted_origins"] == 2
    assert summary["counts"]["planned_origins"] == 3
    assert summary["counts"]["scored_fraction_of_planned_hours"] == pytest.approx(4 / 144)
    assert summary["counts"]["scored_fraction_of_accepted_hours"] == pytest.approx(4 / 96)
    paired = summary["paired_origin_comparisons"]["ecmwf_ifs_c"]
    assert paired["timesfm_wins"] == 1 and paired["timesfm_losses"] == 1
    assert paired["mean_origin_mae_delta_c"] == 1.0
    leads = tables["per_lead.csv"]
    first = leads.loc[leads["method"].eq("timesfm_c") & leads["lead_hours"].eq(1)].iloc[0]
    assert first["n"] == 2 and first["rmse_c"] == pytest.approx(8**0.5)
    assert leads.loc[leads["lead_hours"].eq(48), "n"].eq(0).all()


def test_empty_run_has_no_fake_scores_or_nan_json():
    ledger = pd.DataFrame({"status": ["data_rejected", "inference_error"], "reason": ["history_policy", "RuntimeError"], "target_observed_hours": [48, 48]})
    summary, tables = summarize(ledger, pd.DataFrame(), protocol())
    assert summary["scores"] == {}
    assert summary["counts"]["error_origins"] == 1
    assert summary["counts"]["scored_fraction_of_accepted_hours"] is None
    assert all(table.empty for table in tables.values())
    json.dumps(summary, allow_nan=False)


@pytest.mark.parametrize("problem", ["missing", "incomplete", "changed_protocol", "changed_code", "no_scored_cases"])
def test_holdout_is_gated_on_verified_development(tmp_path, problem):
    path = tmp_path / "manifest.json"
    record = {"run_status": "complete", "protocol_sha256": "digest", "pipeline_sha256": {"a.py": "hash"}, "results": {"counts": {"accepted_origins": 1}}}
    if problem == "incomplete":
        record["run_status"] = "incomplete"
    elif problem == "changed_protocol":
        record["protocol_sha256"] = "different"
    elif problem == "changed_code":
        record["pipeline_sha256"] = {}
    elif problem == "no_scored_cases":
        record["results"]["counts"]["accepted_origins"] = 0
    if problem != "missing":
        path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        require_development(path, "digest", {"a.py": "hash"})


def test_verified_development_allows_holdout(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"run_status": "complete", "protocol_sha256": "digest", "pipeline_sha256": {}, "results": {"counts": {"accepted_origins": 1}}}))
    require_development(path, "digest", {})


def one_eligible_origin():
    index = pd.date_range("2025-05-01", "2025-06-04", freq="h", tz="UTC")
    observed = pd.Series(25.0, index=index)
    audit = audit_origins(observed, protocol()).iloc[:1].copy()
    assert audit.iloc[0]["status"] == "eligible"
    return observed, audit


def test_rejected_origins_do_not_download_or_run_model(tmp_path, monkeypatch):
    observed, audit = one_eligible_origin()
    audit["status"] = "data_rejected"
    audit["reason"] = "history_policy"

    def forbidden(*args, **kwargs):
        raise AssertionError("Rejected origin should not execute")

    monkeypatch.setattr(bt, "load_forecast", forbidden)
    monkeypatch.setattr(bt, "predict_timesfm", forbidden)
    ledger, predictions = bt.evaluate_split(observed, {}, audit, protocol(), tmp_path, "cpu", tmp_path, tmp_path, True)
    assert predictions.empty
    assert ledger["status"].tolist() == ["data_rejected"]


def test_inference_failure_is_not_a_data_rejection_and_removes_stale_forecast(tmp_path, monkeypatch):
    observed, audit = one_eligible_origin()
    monkeypatch.setattr(bt, "load_forecast", lambda *args: (pd.Series(25.0, index=observed.index), {}, {}))

    def fail(*args, **kwargs):
        raise RuntimeError("GPU unavailable")

    monkeypatch.setattr(bt, "predict_timesfm", fail)
    case_dir = tmp_path / "cases" / "20250601T0600Z"
    case_dir.mkdir(parents=True)
    (case_dir / "forecast.csv").write_text("old result")
    (tmp_path / "predictions.csv").write_text("old aggregate")
    ledger, predictions = bt.evaluate_split(observed, {}, audit, protocol(), tmp_path, "cpu", tmp_path, tmp_path, True)
    assert predictions.empty
    assert ledger["status"].tolist() == ["inference_error"]
    assert not (case_dir / "forecast.csv").exists()
    assert pd.read_csv(tmp_path / "predictions.csv").empty
    assert json.loads((case_dir / "manifest.json").read_text())["status"] == "inference_error"


def test_incomplete_archived_run_is_logged_and_not_filled(tmp_path, monkeypatch):
    observed, audit = one_eligible_origin()
    forecast = pd.Series(25.0, index=observed.index)
    forecast.loc["2025-06-01T12:00Z"] = np.nan
    monkeypatch.setattr(bt, "load_forecast", lambda *args: (forecast, {}, {}))

    def forbidden(*args, **kwargs):
        raise AssertionError("An incomplete forecast cannot reach inference")

    monkeypatch.setattr(bt, "predict_timesfm", forbidden)
    ledger, predictions = bt.evaluate_split(observed, {}, audit, protocol(), tmp_path, "cpu", tmp_path, tmp_path, True)
    assert predictions.empty
    assert ledger.iloc[0]["status"] == "data_rejected"
    assert ledger.iloc[0]["reason"] == "incomplete_forecast_horizon"
