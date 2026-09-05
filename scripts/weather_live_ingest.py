"""Collect one live cycle, or poll for new cycles. No cloud provisioning."""

import argparse
import json
import signal
import threading

from timesfm_serve import db, weather_live_store, weather_store
from timesfm_serve.weather_ingest import ProviderError, SourceClient, capture
from timesfm_serve.weather_live_policy import STATIONS, LiveInputError, check_issue_window, current_origin, parse_utc, utc_now


def run_cycle(station_ids=None, origin=None, client=None):
    origin = parse_utc(origin) if origin is not None else current_origin(utc_now())
    try:
        check_issue_window(origin, utc_now())
    except LiveInputError:
        return {"origin": origin.isoformat(), "status": "waiting_for_issue_window", "stations": []}
    results = []
    own_client = client is None
    client = client or SourceClient()
    try:
        for station in station_ids or STATIONS:
            with weather_live_store.station_lock(station) as acquired:
                if not acquired:
                    results.append({"station_id": station, "status": "another_ingestor_active"})
                    continue
                if job_id := weather_live_store.existing_job(station, origin):
                    status, error = "existing", None
                else:
                    job_id = None
                    try:
                        snapshot = capture(station, origin, client=client)
                        job_id, created = weather_live_store.submit_snapshot(snapshot)
                        status, error = ("queued" if created else "existing"), None
                    except ProviderError:
                        status, error = "provider_unavailable", "upstream_unavailable"
                    except LiveInputError as exc:
                        status, error = "data_rejected", str(exc)
                    except (ValueError, KeyError, TypeError):
                        status, error = "data_rejected", "invalid_source_data"
                    except weather_store.SubmissionError:
                        status, error = "queue_full", "queue_full"
                weather_live_store.record_status(station, origin, status, error)
                results.append({"station_id": station, "status": status, "error_code": error, "job_id": str(job_id) if job_id else None})
        return {"origin": origin.isoformat(), "status": "cycle_checked", "stations": results}
    finally:
        if own_client:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", choices=list(STATIONS), action="append")
    parser.add_argument("--origin", help="An eligible six-hour UTC origin; historical windows are rejected")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.interval_seconds < 300 or (args.watch and args.origin):
        parser.error("Poll at most once per 300 seconds; --origin is only supported for a single cycle")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    db.initialize()
    try:
        while not stop.is_set():
            print(json.dumps(run_cycle(args.station, args.origin)), flush=True)
            if not args.watch:
                return
            stop.wait(args.interval_seconds)
    finally:
        db.pool.close()


if __name__ == "__main__":
    main()
