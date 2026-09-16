"""Check the local IEX store against IEX's own aggregates and an outside publisher.

Grid-India republishes every exchange's 15-minute prices in its weekly "PX Rates"
files, so it is the one independent copy of the same numbers.
"""

import argparse
import io
import json
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import certifi
import pandas as pd
import requests
from cryptography.hazmat.primitives import serialization

from iex.data import ROOT

GRID_API = "https://webapi.grid-india.in/api/v1/file"
GRID_CDN = "https://webcdn.grid-india.in/"
# Grid-India serves its leaf certificate without the intermediate, so the chain
# has to be completed from the CA Issuers URL the certificate itself advertises.
GRID_INTERMEDIATE = "http://certificates.godaddy.com/repository/gdig2.crt"
BUNDLE = Path("data/iex/grid-india-chain.pem")
# Grid-India writes -1 in the unified price column when no single price applies;
# the area columns still carry the real value.
SENTINEL = -1.0


def trust_bundle():
    if not BUNDLE.exists():
        import ssl

        from cryptography import x509

        der = requests.get(GRID_INTERMEDIATE, timeout=60).content
        chain = x509.load_der_x509_certificate(der).public_bytes(serialization.Encoding.PEM).decode()
        BUNDLE.parent.mkdir(parents=True, exist_ok=True)
        BUNDLE.write_text(Path(ssl.get_default_verify_paths().cafile or certifi.where()).read_text() + chain)
    return str(BUNDLE)


def get(url, data=None, timeout=120):
    headers = {"User-Agent": "iex-research"}
    verify = trust_bundle() if "grid-india" in url else True
    response = (requests.post(url, json=data, headers=headers, timeout=timeout, verify=verify) if data
                else requests.get(url, headers=headers, timeout=timeout, verify=verify))
    response.raise_for_status()
    return response.content


def iex_aggregate(market, interval, first, last):
    query = urlencode({"interval": interval, "fromDate": first.strftime("%d-%m-%Y"), "toDate": last.strftime("%d-%m-%Y")})
    return json.loads(get(f"https://www.iexindia.com/api/v1/{market}/market-snapshot?{query}"))["data"]


def check_aggregates(frame, market, first, last):
    """Our 96 blocks must reproduce the daily and hourly figures IEX publishes."""
    worst_daily = worst_hourly = 0.0
    for row in iex_aggregate(market, "DAILY", first, last):
        day = datetime.strptime(row["date"], "%d-%m-%Y").date()
        ours = frame[frame["delivery_date"] == day]["price"]
        if len(ours) == 96:
            worst_daily = max(worst_daily, abs(float(row["mcp"]) - ours.mean()))
    for row in iex_aggregate(market, "ONE_HOUR", first, first):
        hour = int(row["hour"]) - (0 if market == "rtm" else 1)
        ours = frame[(frame["delivery_date"] == first) & frame["block"].between(hour * 4 + 1, hour * 4 + 4)]["price"]
        if len(ours) == 4:
            worst_hourly = max(worst_hourly, abs(float(row["mcp"]) - ours.mean()))
    return worst_daily, worst_hourly


def grid_india_weeks(year="2026-27"):
    payload = json.loads(get(GRID_API, {"_source": "GRDW", "_type": "F_PG00403", "_fileDate": year}))
    items = payload if isinstance(payload, list) else next(v for v in payload.values() if isinstance(v, list))
    return [item for item in items if "PX Rates" in item.get("Title_", "")]


def compare_grid_india(store, entry):
    rows = []
    archive = zipfile.ZipFile(io.BytesIO(get(GRID_CDN + entry["FilePath"])))
    for name in archive.namelist():
        if not name.endswith(".xlsx"):
            continue
        day = datetime.strptime(Path(name).stem.split("PX Rates ")[-1], "%d-%m-%Y").date()
        content = io.BytesIO(archive.read(name))
        for market in ("dam", "rtm", "gdam"):
            frame = store.get(market)
            if frame is None:
                continue
            try:
                sheet = pd.read_excel(content, sheet_name=f"{market.upper()}_IEX", header=0)
            except ValueError:
                continue
            theirs = sheet.set_index(sheet.columns[0])["UMCP"].astype(float)
            theirs = theirs[theirs != SENTINEL]
            ours = frame[frame["delivery_date"] == day].set_index("block")["price"]
            common = theirs.index.intersection(ours.index)
            if not len(common):
                continue
            difference = (theirs.loc[common] - ours.loc[common]).abs()
            rows.append({
                "day": day, "market": market, "compared": len(common),
                "sentinels": int((sheet.set_index(sheet.columns[0])["UMCP"].astype(float) == SENTINEL).sum()),
                "mismatches": int((difference > 0.01).sum()), "max_diff": float(difference.max()),
            })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets", default="dam,rtm,gdam")
    parser.add_argument("--aggregate-month", type=date.fromisoformat, default=date(2025, 6, 1))
    parser.add_argument("--grid-india-weeks", type=int, default=1)
    args = parser.parse_args()

    store = {m: pd.read_parquet(ROOT / f"{m}_15m.parquet") for m in args.markets.split(",")}
    first = args.aggregate_month.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    print(f"IEX aggregates, {first} to {last}")
    for market, frame in store.items():
        daily, hourly = check_aggregates(frame, market, first, last)
        status = "ok" if max(daily, hourly) < 0.01 else "MISMATCH"
        print(f"  {market:5} daily max diff {daily:.4f}  hourly max diff {hourly:.4f}  {status}")

    print("\nGrid-India PX Rates, an independent copy of the same prices")
    results = []
    for entry in grid_india_weeks()[: args.grid_india_weeks]:
        results.extend(compare_grid_india(store, entry))
    summary = pd.DataFrame(results).groupby("market")[["compared", "sentinels", "mismatches", "max_diff"]].agg(
        {"compared": "sum", "sentinels": "sum", "mismatches": "sum", "max_diff": "max"})
    for market, row in summary.iterrows():
        rate = 100 * (1 - row["mismatches"] / row["compared"])
        print(f"  {market:5} {int(row['compared']):>5} blocks  {int(row['mismatches']):>3} mismatched  "
              f"{rate:6.2f}% agree  max diff {row['max_diff']:.2f}  ({int(row['sentinels'])} sentinels skipped)")


if __name__ == "__main__":
    main()
