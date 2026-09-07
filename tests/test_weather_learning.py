import copy
import json
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
    trusted = {"training_windows": 24, "training_origin_span_days": 32.,
               "max_standardized_feature": 1.2, "max_standardized_feature_limit": 6.,
               "in_distribution": True}
    report = evaluate(validation, {e["id"]: [30.] * 48 for e in validation})
    assert review_gate(report, ridge=trusted)["status"] == "review_required"
    assert review_gate(report, ridge=trusted)["automatic_promotion"] is False
    assert review_gate(report, engineering_smoke=True, ridge=trusted)["status"] == "rejected"
    bad = evaluate(validation, {e["id"]: [34.] * 48 for e in validation})
    assert review_gate(bad, ridge=trusted)["status"] == "rejected"


def test_candidate_is_not_graded_against_an_untrustworthy_ridge():
    validation = examples()[:12]
    trusted = {"training_windows": 24, "training_origin_span_days": 32.,
               "max_standardized_feature": 1.2, "max_standardized_feature_limit": 6.,
               "in_distribution": True}
    report = evaluate(validation, {e["id"]: [30.] * 48 for e in validation})
    assert review_gate(report)["checks"]["ridge_comparator_valid"] is False
    assert review_gate(report)["status"] == "rejected"
    extrapolating = review_gate(report, ridge={**trusted, "in_distribution": False,
                                               "max_standardized_feature": 22.4})
    assert extrapolating["checks"]["ridge_comparator_valid"] is False
    assert extrapolating["status"] == "rejected"
    # A correction scoring worse than the guidance it corrects is not a bar either.
    worthless = [e | {"ridge": [20.] * 48} for e in validation]
    degenerate = evaluate(worthless, {e["id"]: [30.] * 48 for e in worthless})
    assert degenerate["overall"]["ridge"]["rmse_c"] > degenerate["overall"]["ecmwf"]["rmse_c"]
    assert review_gate(degenerate, ridge=trusted)["checks"]["ridge_comparator_valid"] is False
    assert review_gate(degenerate, ridge=trusted)["status"] == "rejected"


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


def deployment_config():
    import json
    from pathlib import Path

    config = json.loads(Path("deploy/example.json").read_text())
    config["infrastructure"].update(gpu_nodes=1, learning_enabled=True)
    return config


def test_scoring_runs_off_the_gpu_on_a_standard_node():
    documents = render(deployment_config(), enable_worker=True, enable_live=True, example=True)
    score = next(r for r in documents["03-workloads.json"] if r["metadata"]["name"] == "weather-score")
    spec = score["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert score["spec"]["schedule"] == "17 * * * *" and score["spec"]["concurrencyPolicy"] == "Forbid"
    assert spec["nodeSelector"]["workload"] == "standard" and "tolerations" not in spec
    assert spec["containers"][0]["command"] == ["python", "-m", "scripts.weather_score"]
    assert "nvidia.com/gpu" not in spec["containers"][0]["resources"]["limits"]
    # Reusing the ingest identity keeps scoring inside existing IAM and network policy.
    assert spec["serviceAccountName"] == "weather-ingest"
    # The GPU supervisor must no longer hold an L4 open to run numpy once an hour.
    from pathlib import Path

    from timesfm_serve import weather_gpu
    assert "refresh_scores" not in Path(weather_gpu.__file__).read_text()


def test_autoscaling_scales_one_gpu_to_zero_and_excludes_learning():
    config = deployment_config()
    config["autoscaling"] = {"cooldown_seconds": 900, "trigger_authentication": "weather-keda-db"}
    documents = render(config, enable_worker=True, enable_live=True, example=True)
    scaled = next(r for r in documents["03-workloads.json"] if r["kind"] == "ScaledObject")
    assert scaled["spec"]["scaleTargetRef"]["name"] == "weather-worker"
    assert scaled["spec"]["idleReplicaCount"] == 0 and scaled["spec"]["maxReplicaCount"] == 1
    assert scaled["spec"]["triggers"][0]["authenticationRef"]["name"] == "weather-keda-db"
    assert "password" not in json.dumps(documents).lower()
    for cooldown in (60, 7200):
        broken = deployment_config() | {"autoscaling": {"cooldown_seconds": cooldown, "trigger_authentication": "weather-keda-db"}}
        with pytest.raises(ValueError, match="cooldown|five minutes"):
            render(broken, enable_worker=True, enable_live=True, example=True)
    # A scaled-to-zero worker has no supervisor, so the weekly run would vanish.
    config["learning"] = {"training_mode": "research", "max_seconds": 600, "max_steps": 64}
    with pytest.raises(ValueError, match="not both"):
        render(config, enable_worker=True, enable_live=True, example=True)


def test_alerts_only_reference_metrics_the_api_exports():
    from timesfm_serve import weather_metrics

    config = deployment_config()
    account = config["infrastructure"]["runtime_secrets"]["api"].split(":")[4]
    config["metrics"] = {"secret_arn": f"arn:aws:secretsmanager:us-east-1:{account}:secret:pravah/metrics-AbCdEf",
                         "scrape_secret": "weather-metrics"}
    documents = render(config, enable_worker=True, enable_live=True, example=True)
    monitor = next(r for r in documents["03-workloads.json"] if r["kind"] == "ServiceMonitor")
    assert monitor["spec"]["endpoints"][0]["authorization"]["credentials"] == {"name": "weather-metrics", "key": "token"}
    rules = next(r for r in documents["03-workloads.json"] if r["kind"] == "PrometheusRule")["spec"]["groups"][0]["rules"]
    exported = {name for name in dir(weather_metrics)
                if hasattr(getattr(weather_metrics, name), "_name")}
    exported = {getattr(weather_metrics, name)._name for name in exported}
    for rule in rules:
        assert rule["annotations"]["summary"] and rule["labels"]["severity"] in ("warning", "critical")
        assert any(metric in rule["expr"] for metric in exported), rule["alert"]
    # The token is a mounted file, never a rendered value.
    api = next(r for r in documents["03-workloads.json"]
               if r["kind"] == "Deployment" and r["metadata"]["name"] == "weather-api")["spec"]["template"]["spec"]
    assert {"name": "WEATHER_METRICS_TOKEN_FILE", "value": "/run/weather/metrics-token"} in api["containers"][0]["env"]
    assert "WEATHER_METRICS_TOKEN_ARN" in [e["name"] for e in api["initContainers"][0]["env"]]
