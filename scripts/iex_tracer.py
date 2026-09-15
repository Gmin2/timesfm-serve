"""Tracer bullet: forecast one IEX day-ahead delivery day, naive vs TimesFM 3.0.

The forecast is issued at 09:30 IST on the day before delivery. At that point the
day-ahead prices for every earlier delivery day are published and nothing about
the delivery day itself is, so history ends at block 96 of the previous day.
"""

import argparse
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np

from scripts.weather_model import MODEL_REVISION

API = "https://www.iexindia.com/api/v1/dam/market-snapshot"
RAW = Path("data/iex/raw")
BLOCKS = 96
CAP = 10000.0


def fetch(first, last):
    query = urlencode({"interval": "ONE_FOURTH_HOUR", "fromDate": first.strftime("%d-%m-%Y"), "toDate": last.strftime("%d-%m-%Y")})
    request = Request(f"{API}?{query}", headers={"User-Agent": "iex-tracer"})
    with urlopen(request, timeout=60) as response:
        body = response.read()
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / f"dam_15m_{first:%Y%m%d}_{last:%Y%m%d}.json").write_bytes(body)
    payload = json.loads(body)
    if payload.get("statusCode") != 200:
        raise RuntimeError(f"IEX returned {payload.get('statusCode')}: {payload.get('message')}")
    return payload["data"]


def daily_prices(rows):
    days = {}
    for row in rows:
        day = datetime.strptime(row["date"], "%d-%m-%Y").date()
        hour, minute = map(int, row["period"].split("-")[0].strip().split(":"))
        days.setdefault(day, [None] * BLOCKS)[hour * 4 + minute // 15] = float(row["mcp"])
    for day, prices in days.items():
        if None in prices:
            raise ValueError(f"{day} is missing {prices.count(None)} blocks")
    return days


def load_model(cache_dir):
    import timesfm
    import torch
    from huggingface_hub import snapshot_download

    device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
    path = snapshot_download("google/timesfm-3.0-pytorch", revision=MODEL_REVISION, cache_dir=cache_dir, local_files_only=True)
    return timesfm.TimesFM3Forecaster.from_pretrained(path, device=device, local_files_only=True), device


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", type=date.fromisoformat, default=date(2024, 11, 12))
    parser.add_argument("--context-days", type=int, default=28)
    parser.add_argument("--cache-dir", default=os.environ.get("HF_HUB_CACHE"))
    args = parser.parse_args()

    issued_at = datetime.combine(args.day - timedelta(days=1), datetime.min.time()).replace(hour=9, minute=30)
    history = daily_prices(fetch(args.day - timedelta(days=args.context_days), args.day - timedelta(days=1)))
    if max(history) >= args.day:
        raise AssertionError("history contains the delivery day")
    expected = [args.day - timedelta(days=n) for n in range(args.context_days, 0, -1)]
    if sorted(history) != expected:
        raise ValueError("history is not a complete run of consecutive days")
    context = np.array([price for day in expected for price in history[day]])

    actual = np.array(daily_prices(fetch(args.day, args.day))[args.day])
    naive = np.array(history[args.day - timedelta(days=1)])

    model, device = load_model(args.cache_dir)
    started = time.perf_counter()
    output = model.predict(context, horizon=BLOCKS, return_quantiles=True)
    seconds = time.perf_counter() - started
    timesfm_forecast = np.clip(np.asarray(output.forecast, dtype=float), 0, CAP)

    print(f"delivery day {args.day}, issued {issued_at:%Y-%m-%d %H:%M} IST, context {len(context)} blocks ending {expected[-1]} 24:00")
    print(f"timesfm 3.0 on {device}, {seconds:.2f}s\n")
    print(f"{'block':<14}{'actual':>10}{'naive':>10}{'timesfm':>10}   (Rs/MWh)")
    for block in (0, 28, 52, 76, 95):
        start = f"{block // 4:02d}:{block % 4 * 15:02d}"
        print(f"{start:<14}{actual[block]:>10.2f}{naive[block]:>10.2f}{timesfm_forecast[block]:>10.2f}")
    print()
    for name, forecast in (("naive (yesterday)", naive), ("timesfm 3.0 zero-shot", timesfm_forecast)):
        mae = np.abs(forecast - actual).mean()
        print(f"{name:<24} MAE {mae:8.1f} Rs/MWh  ({mae / 1000:.2f} Rs/unit)")


if __name__ == "__main__":
    main()
