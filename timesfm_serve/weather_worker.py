"""One warm GPU model, bounded jobs, fenced publication and recoverable leases."""

import argparse
import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

from timesfm_serve import db, weather_store
from timesfm_serve.weather_engine import WeatherEngine

logger = logging.getLogger("weather-worker")
READY_FILE = Path(os.environ.get("WEATHER_WORKER_READY_FILE", "/tmp/weather-worker-ready"))
JOB_TIMEOUT_SECONDS = 90


def process_one(engine):
    weather_store.recover_expired()
    job = weather_store.claim()
    if job is None:
        return False

    def timeout():
        logger.error(json.dumps({"event": "weather_job_timeout", "job_id": str(job["id"])}))
        READY_FILE.unlink(missing_ok=True)
        # A hung CUDA call cannot safely be cancelled in a thread. The supervisor
        # restarts this process; another worker recovers the expired DB lease.
        os._exit(1)

    watchdog = threading.Timer(JOB_TIMEOUT_SECONDS, timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        try:
            document = engine.predict(job)
            published = weather_store.complete(job, document)
            event = "weather_job_succeeded" if published else "weather_job_lease_lost"
        except (ValueError, KeyError, FileNotFoundError):
            code = "invalid_live_inputs" if job.get("kind") == "live" else "invalid_replay"
            weather_store.fail(job, code, retryable=False)
            event = f"weather_job_{code}"
        except Exception as exc:
            logger.error(json.dumps({"event": "weather_inference_error", "exception_type": type(exc).__name__}))
            weather_store.fail(job)
            event = "weather_job_retry_or_failure"
        logger.info(json.dumps({"event": event, "job_id": str(job["id"]), "attempt": job["attempts"]}))
    finally:
        watchdog.cancel()
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=("cuda", "mps"), default=os.environ.get("DEVICE", "cuda"))
    parser.add_argument("--cache-dir", default=os.environ.get("HF_HUB_CACHE"))
    parser.add_argument("--once", action="store_true", help="Process at most one queued job, then exit")
    parser.add_argument("--recover-only", action="store_true", help="Recover expired leases without loading a model")
    parser.add_argument("--check-ready", action="store_true", help="Check the worker's recent progress marker")
    args = parser.parse_args()
    if args.check_ready:
        try:
            fresh = time.time() - READY_FILE.stat().st_mtime < JOB_TIMEOUT_SECONDS + 10
        except FileNotFoundError:
            fresh = False
        raise SystemExit(0 if fresh else 1)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if not args.recover_only:
        READY_FILE.unlink(missing_ok=True)
    db.initialize()
    try:
        if args.recover_only:
            logger.info(json.dumps({"event": "weather_leases_recovered", "count": weather_store.recover_expired()}))
            return
        engine = WeatherEngine(args.device, args.cache_dir)
        engine.warmup()
        while not stop.is_set():
            READY_FILE.touch()
            processed = process_one(engine)
            if args.once:
                break
            if not processed:
                stop.wait(2)
    finally:
        if not args.recover_only:
            READY_FILE.unlink(missing_ok=True)
        db.pool.close()


if __name__ == "__main__":
    main()
