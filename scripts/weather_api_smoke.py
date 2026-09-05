"""Exercise the running API and separate worker; never starts cloud resources."""

import argparse
import csv
import json
import os
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from timesfm_serve.weather_catalog import case_manifest
from timesfm_serve.weather_contract import ForecastDocument


def smoke(base_url, api_key, case_id, timeout=150, verify_reference=False):
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
        raise ValueError("Use HTTPS, except for a local smoke test")
    base_url = base_url.rstrip("/")
    idempotency_key = str(uuid.uuid4())

    def call(path, body=None):
        request = Request(base_url + path, data=json.dumps(body).encode() if body is not None else None, headers={
            "x-api-key": api_key, "idempotency-key": idempotency_key, "content-type": "application/json",
        })
        with urlopen(request, timeout=10) as response:
            return response.status, json.load(response)

    payload = {"case_id": case_id}
    first_status, first = call("/v1/weather/replays", payload)
    second_status, second = call("/v1/weather/replays", payload)
    if (first_status, second_status) != (202, 200) or first["job_id"] != second["job_id"]:
        raise RuntimeError("Idempotent submission check failed")
    job_id = first["job_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, job = call(f"/v1/weather/jobs/{job_id}")
        if job["status"] == "failed":
            raise RuntimeError(f"Replay failed: {job['error_code']}; job_id={job_id}")
        if job["status"] == "succeeded":
            break
        time.sleep(2)
    else:
        raise TimeoutError(f"Worker did not finish within {timeout}s; job_id={job_id}")
    _, output = call(f"/v1/weather/forecasts/{job_id}")
    forecast = ForecastDocument.model_validate(output)
    for _ in range(3):
        _, reread = call(f"/v1/weather/forecasts/{job_id}")
        if reread != output:
            raise RuntimeError("Stored forecast changed across reads")
    report = {
        "job_id": job_id, "case_id": case_id, "status": job["status"], "mode": forecast.mode,
        "device": forecast.model_provenance["device"], "points": len(forecast.points),
        "attempts": job["attempts"], "credits_reserved": job["credits_reserved"],
        "idempotency_verified": True, "stable_reads_verified": True,
    }
    if verify_reference:
        directory, _, _ = case_manifest(case_id)
        with (directory / "predictions.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        if len(rows) != 48:
            raise ValueError("Expected 48 reference rows")
        deltas = []
        for point, row in zip(forecast.points, rows, strict=True):
            deltas.append(abs(point.temperature_2m - float(row["timesfm_nwp_c"])))
            deltas.extend(abs(q - float(row[f"timesfm_nwp_p{level}_c"])) for q, level in zip(point.quantiles, range(10, 100, 10), strict=True))
        report["reference_max_absolute_difference_c"] = max(deltas)
        if max(deltas) >= 1e-4:
            raise ValueError(f"Replay diverged from the saved reference: {max(deltas)} C")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--case-id", default="42410099999_20260818T0600Z")
    parser.add_argument("--timeout", type=int, default=150)
    parser.add_argument("--verify-reference", action="store_true")
    args = parser.parse_args()
    key = os.environ.get("WEATHER_API_KEY")
    if not key:
        parser.error("Set WEATHER_API_KEY; keys are not accepted in command-line arguments")
    print(json.dumps(smoke(args.base_url, key, args.case_id, args.timeout, args.verify_reference), indent=2))


if __name__ == "__main__":
    main()
