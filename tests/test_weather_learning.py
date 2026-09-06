import copy
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

from scripts.weather_render import render
from timesfm_serve.weather_gpu import exclusive_training, maintenance_window, stop_child
from timesfm_serve.weather_learning import dataset, metrics, validate_dataset
from timesfm_serve.weather_live_policy import STATIONS
from timesfm_serve.weather_training import evaluate, review_gate


def examples():
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [{"id": f"{station}-{day}", "station_id": station, "origin": (origin + timedelta(days=day)).isoformat(),
             "context": [30.] * 168, "observed_context": [30.] * 168, "covariate": [32.] * 216,
             "target_available_at": [(origin + timedelta(days=day, hours=lead + 1)).isoformat() for lead in range(1, 49)],
             "targets": [30.] * 48, "current": [31.] * 48, "ecmwf": [32.] * 48, "ridge": [31.5] * 48}
            for day in range(40) for station in STATIONS]


def test_delayed_labels_purged_splits_and_missing_targets():
    document = dataset(examples(), datetime(2026, 2, 12, tzinfo=timezone.utc))
    validate_dataset(document)
    assert {e["split"] for e in document["examples"]} == {"train", "validation"}
    broken = copy.deepcopy(document)
    broken["examples"][0]["origin"] = document["validation_start"]
    with pytest.raises(ValueError, match="overlap"):
        validate_dataset(broken)
    broken = copy.deepcopy(document)
    broken["examples"][0]["target_available_at"][0] = document["as_of"]
    with pytest.raises(ValueError, match="available"):
        validate_dataset(broken)
    broken = copy.deepcopy(document)
    broken["examples"][0]["targets"] = [None] * 48
    with pytest.raises(ValueError, match="observed targets"):
        validate_dataset(broken)
    assert metrics([30, None, 32], [29, 100, 34])["hours"] == 2


def test_candidate_gate_never_automatically_promotes():
    validation = examples()[:12]
    report = evaluate(validation, {e["id"]: [30.] * 48 for e in validation})
    assert review_gate(report)["status"] == "review_required"
    assert review_gate(report)["automatic_promotion"] is False
    assert review_gate(report, engineering_smoke=True)["status"] == "rejected"
    bad = evaluate(validation, {e["id"]: [34.] * 48 for e in validation})
    assert review_gate(bad)["status"] == "rejected"


@pytest.mark.parametrize("fails", [False, True])
def test_exclusive_gpu_handoff_stops_inference_before_training(tmp_path, fails):
    inference = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    flag = tmp_path / "trained"
    command = [sys.executable, "-c",
               "import os\nfrom pathlib import Path\n"
               f"try:\n    os.kill({inference.pid}, 0)\n"
               "except ProcessLookupError:\n    pass\n"
               "else:\n    raise AssertionError('Inference was still alive when training started')\n"
               f"Path({str(flag)!r}).write_text('ok')\n" +
               ("import time; time.sleep(30)" if fails else "pass")]
    try:
        if fails:
            with pytest.raises(TimeoutError):
                exclusive_training(inference, command, .1, threading.Event(), lambda: None)
        else:
            exclusive_training(inference, command, 5, threading.Event(), lambda: None)
        assert inference.poll() is not None
        assert flag.read_text() == "ok"
    finally:
        stop_child(inference, grace=1)


def test_learning_render_preserves_single_gpu_and_forecast_window():
    import json
    from pathlib import Path

    config = json.loads(Path("deploy/example.json").read_text())
    config["infrastructure"].update(gpu_nodes=1, learning_enabled=True)
    config["learning"] = {"training_mode": "research", "max_seconds": 600, "max_steps": 64}
    documents = render(config, enable_worker=True, enable_live=True, example=True)
    deployments = [r for r in documents["03-workloads.json"] if r["kind"] == "Deployment"]
    worker = next(r for r in deployments if r["metadata"]["name"] == "weather-worker")
    assert worker["spec"]["replicas"] == 1 and worker["spec"]["strategy"] == {"type": "Recreate"}
    container = worker["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert "timesfm_serve.weather_gpu" in container["command"]
    assert not maintenance_window(datetime(2026, 9, 6, 13, tzinfo=timezone.utc), 600)
    assert maintenance_window(datetime(2026, 9, 6, 16, tzinfo=timezone.utc), 600)
