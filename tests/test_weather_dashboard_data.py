import json
import math

import pytest

from scripts.weather_dashboard_data import SOURCE, case_document, export, number


def test_export_matches_holdout_and_preserves_gaps(tmp_path):
    catalog = export(tmp_path)
    assert len(catalog["cases"]) == 90
    assert len(catalog["runs"]) == 99
    assert catalog["benchmark"]["counts"]["scored_hours"] == 4271
    observed = 0
    errors = []
    for case in catalog["cases"]:
        document = json.loads((tmp_path / "cases" / f"{case['id']}.json").read_text())
        assert document["mode"] == "historical_replay"
        assert document["quantilesCalibrated"] is False
        assert len(document["points"]) == 48
        for point in document["points"]:
            if point["observed"] is not None:
                observed += 1
                errors.append((point["forecast"] - point["observed"]) ** 2)
    assert observed == 4271
    score = next(score for score in catalog["benchmark"]["scores"] if score["method"] == "forecast")
    assert math.sqrt(sum(errors) / len(errors)) == pytest.approx(score["rmse_c"])


def test_artifact_tampering_is_rejected(tmp_path):
    source = SOURCE / "cases/42410099999_20260818T0600Z"
    (tmp_path / "manifest.json").write_bytes((source / "manifest.json").read_bytes())
    (tmp_path / "predictions.csv").write_bytes((source / "predictions.csv").read_bytes() + b"\n")
    with pytest.raises(ValueError, match="checksum"):
        case_document(tmp_path)


def test_missing_is_not_zero():
    assert number("") is None
    assert number("0") == 0
    with pytest.raises(ValueError):
        number("nan")
