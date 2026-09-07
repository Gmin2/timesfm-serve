"""One GPU allocation, sequential inference/training children, bounded recovery."""

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

from timesfm_serve import db
from timesfm_serve.weather_live_policy import STATIONS, parse_utc, utc_now
from timesfm_serve.weather_worker import READY_FILE

logger = logging.getLogger("weather-gpu")
INFERENCE_READY = Path("/tmp/weather-inference-ready")
SUPERVISOR_LOCK = 8_274_122


class GpuReleaseError(RuntimeError):
    pass


def stop_child(process, grace=100):
    if process is None:
        return
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired as error:
                raise GpuReleaseError("GPU process did not exit; refusing another GPU process") from error
    else:
        process.wait()


def start_inference():
    INFERENCE_READY.unlink(missing_ok=True)
    return subprocess.Popen([sys.executable, "-m", "timesfm_serve.weather_worker"], start_new_session=True,
                            env=os.environ | {"WEATHER_WORKER_READY_FILE": str(INFERENCE_READY)})


def exclusive_training(inference, command, budget, stop, heartbeat=READY_FILE.touch):
    """No training process exists until the inference process has fully exited."""
    stop_child(inference)
    if stop.is_set():
        raise InterruptedError("GPU supervisor is stopping")
    child = subprocess.Popen(command, start_new_session=True, env=os.environ | {"WEATHER_TRAINING_CHILD": "1"})
    deadline = time.monotonic() + budget
    try:
        while child.poll() is None:
            heartbeat()
            if stop.wait(.5):
                raise InterruptedError("Training interrupted by shutdown")
            if time.monotonic() >= deadline:
                raise TimeoutError("Training exceeded its GPU reservation")
        if child.returncode:
            raise RuntimeError(f"Training process exited with code {child.returncode}")
    finally:
        stop_child(child, grace=5)


def maintenance_window(now, budget):
    """Avoid the three-hour forecast issue window and leave ten minutes to reload."""
    now = parse_utc(now)
    cycle = now.replace(hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0)
    return cycle + timedelta(hours=3) <= now and now + timedelta(seconds=budget + 600) < cycle + timedelta(hours=6)


def forecast_capacity_available(now, budget):
    from timesfm_serve.weather_live_store import latest

    with db.conn() as connection:
        if connection.execute("select count(*) from weather_jobs where status in ('queued','running')").fetchone()[0]:
            return False
    for station in STATIONS:
        forecast, _ = latest(station)
        if forecast is None or parse_utc(forecast["fresh_until"]) <= now + timedelta(seconds=budget + 600):
            return False
    return True


def prepare_run(directory, now):
    """Build a training dataset. Scoring belongs to the weather-score CronJob."""
    from psycopg.types.json import Jsonb

    from timesfm_serve.weather_learning import dataset, digest, load_live_examples

    if os.environ.get("WEATHER_TRAINING_MODE", "off") != "research":
        return None
    with db.conn() as connection:
        recent = connection.execute("select 1 from weather_training_runs where created_at > %s - interval '7 days' limit 1", (now,)).fetchone()
    if recent:
        return None
    try:
        document = dataset(load_live_examples(now), now)
    except ValueError as error:
        logger.info(json.dumps({"event": "weather_training_deferred", "reason": str(error)}))
        return None
    dataset_hash = digest(document)
    run_id = uuid.uuid4()
    root = directory / str(run_id)
    path = root / "dataset.json"
    try:
        root.mkdir(parents=True)
        path.write_text(json.dumps(document, allow_nan=False))
        with db.conn() as connection:
            row = connection.execute(
                "insert into weather_training_runs (id, dataset_sha256, status, report) values (%s,%s,'running',%s)"
                " on conflict (dataset_sha256) do nothing returning id",
                (run_id, dataset_hash, Jsonb({"purpose": "research_only", "as_of": now.isoformat(), "automatic_promotion": False})),
            ).fetchone()
        if row is None:
            shutil.rmtree(root)
            return None
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return run_id, path, root / "candidate"


def finish_run(run_id, status, report, prefix=None):
    from psycopg.types.json import Jsonb

    with db.conn() as connection:
        connection.execute(
            "update weather_training_runs set status=%s, finished_at=clock_timestamp(), report=%s, artifact_prefix=%s where id=%s",
            (status, Jsonb(report), prefix, run_id),
        )


def archive_run(run_id, dataset_path, output):
    from scripts.weather_cloud_init import aws_client

    bucket = os.environ["ARTIFACT_BUCKET"]
    prefix = f"training/{run_id}/"
    client = aws_client("s3")
    for path in (dataset_path, output / "report.json", output / "output-head.safetensors"):
        READY_FILE.touch()
        if path.stat().st_size > 32 * 1024**2:
            raise ValueError("Training artifact exceeds its upload budget")
        with path.open("rb") as stream:
            client.put_object(Bucket=bucket, Key=prefix + path.name, Body=stream, ServerSideEncryption="AES256")
    return f"s3://{bucket}/{prefix}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(os.environ.get("WEATHER_TRAINING_DIR", "/learning")))
    parser.add_argument("--max-seconds", type=int, default=600)
    parser.add_argument("--max-steps", type=int, default=64)
    args = parser.parse_args()
    if not 30 <= args.max_seconds <= 900 or not 1 <= args.max_steps <= 256:
        parser.error("Training budget must be 30-900 seconds and 1-256 steps")
    if os.environ.get("WEATHER_TRAINING_MODE", "off") not in ("off", "research"):
        parser.error("Only off or non-production research training is supported")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    db.initialize()
    inference, next_check = None, 0
    try:
        with db.conn() as lock:
            if not lock.execute("select pg_try_advisory_lock(%s)", (SUPERVISOR_LOCK,)).fetchone()[0]:
                raise RuntimeError("Another GPU supervisor owns the maintenance lock")
            try:
                lock.execute("update weather_training_runs set status='interrupted', finished_at=clock_timestamp() where status='running'")
                args.directory.mkdir(parents=True, exist_ok=True)
                for path in args.directory.iterdir():
                    if path.is_dir() and len(path.name) == 36:
                        shutil.rmtree(path)
                inference = start_inference()
                inference_started = time.monotonic()
                while not stop.is_set():
                    lock.execute("select 1")
                    if time.monotonic() - inference_started > 300:
                        age = time.time() - INFERENCE_READY.stat().st_mtime if INFERENCE_READY.exists() else float("inf")
                        if age > 100:
                            logger.error(json.dumps({"event": "weather_inference_stalled"}))
                            stop_child(inference, grace=5)
                    if inference.poll() is not None:
                        logger.warning(json.dumps({"event": "weather_inference_restarting", "exit_code": inference.returncode}))
                        if stop.wait(5):
                            break
                        inference = start_inference()
                        inference_started = time.monotonic()
                    READY_FILE.touch()
                    now = utc_now()
                    if time.monotonic() >= next_check:
                        next_check = time.monotonic() + 3600
                        try:
                            # Claim training only when inference can safely pause.
                            run = (prepare_run(args.directory, now)
                                   if maintenance_window(now, args.max_seconds) and forecast_capacity_available(now, args.max_seconds)
                                   else None)
                            if run:
                                run_id, dataset_path, output = run
                                command = [sys.executable, "-m", "timesfm_serve.weather_training", "--dataset", str(dataset_path),
                                           "--output", str(output), "--max-steps", str(args.max_steps), "--max-seconds", str(args.max_seconds)]
                                try:
                                    logger.info(json.dumps({"event": "weather_gpu_training_started", "run_id": str(run_id)}))
                                    try:
                                        exclusive_training(inference, command, args.max_seconds, stop)
                                    except GpuReleaseError:
                                        stop.set()
                                        raise
                                    finally:
                                        if not stop.is_set():
                                            inference = start_inference()
                                            inference_started = time.monotonic()
                                            logger.info(json.dumps({"event": "weather_gpu_inference_resumed", "run_id": str(run_id)}))
                                    report = json.loads((output / "report.json").read_text())
                                    prefix = archive_run(run_id, dataset_path, output)
                                    finish_run(run_id, report["decision"]["status"], report, prefix)
                                except Exception as error:
                                    status = "timed_out" if isinstance(error, TimeoutError) else "interrupted" if isinstance(error, InterruptedError) else "failed"
                                    finish_run(run_id, status, {"error_type": type(error).__name__, "automatic_promotion": False})
                                    logger.exception("Training failed; the serving checkpoint is unchanged")
                                finally:
                                    shutil.rmtree(dataset_path.parent, ignore_errors=True)
                        except Exception:
                            logger.exception("Learning check failed; inference remains enabled")
                    stop.wait(5)
            finally:
                lock.execute("select pg_advisory_unlock(%s)", (SUPERVISOR_LOCK,))
    finally:
        stop_child(inference)
        READY_FILE.unlink(missing_ok=True)
        db.pool.close()


if __name__ == "__main__":
    main()
