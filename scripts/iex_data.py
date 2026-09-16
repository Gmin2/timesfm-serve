"""Pull IEX 15-minute market data into a local parquet store.

Raw responses are kept verbatim next to the normalised table so any row can be
traced back to the bytes it came from. IEX terms allow personal, non-commercial
use only, so data/iex/ stays out of git.
"""

import argparse
import hashlib
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path("data/iex")
RAW = ROOT / "raw"
BLOCKS = 96

MARKETS = {
    "dam": {"price": "mcp", "start": date(2012, 4, 1)},
    "rtm": {"price": "mcp", "start": date(2020, 6, 1)},
    "gdam": {"price": "unconstrained_m_c_p", "start": date(2021, 10, 27)},
    "hpdam": {"price": "mcp", "start": date(2023, 3, 11)},
}
# CERC capped bids at Rs 12/kWh from delivery day 3 Apr 2022 (4/SM/2022) and at
# Rs 10 from 4 Apr 2023 (04/SM/2023). HP-DAM keeps a Rs 20 ceiling.
CAPS = ((date(2023, 4, 4), 10_000.0), (date(2022, 4, 3), 12_000.0), (date(2012, 4, 1), 20_000.0))
VOLUMES = ("purchase_bid", "sell_bid", "mcv", "final_scheduled_volume", "total_sell_bid", "total_cleared_volume")


def cap_for(day, market):
    if market == "hpdam":
        return 20_000.0
    return next(cap for start, cap in CAPS if day >= start)


def months(first, last):
    start = first.replace(day=1)
    while start <= last:
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        yield max(start, first), min(end, last)
        start = end + timedelta(days=1)


def fetch(market, first, last, pause=1.0, attempts=3):
    """Return the raw response bytes for one date range, caching them on disk."""
    path = RAW / market / f"{market}_15m_{first:%Y%m%d}_{last:%Y%m%d}.json"
    if path.exists():
        return path.read_bytes(), True
    query = urlencode({"interval": "ONE_FOURTH_HOUR", "fromDate": first.strftime("%d-%m-%Y"), "toDate": last.strftime("%d-%m-%Y")})
    url = f"https://www.iexindia.com/api/v1/{market}/market-snapshot?{query}"
    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers={"User-Agent": "iex-research"}), timeout=120) as response:
                body = response.read()
            break
        except (HTTPError, URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(5 * 2**attempt)
    payload = json.loads(body)
    if payload.get("statusCode") != 200:
        raise RuntimeError(f"{market} {first}..{last}: {payload.get('statusCode')} {payload.get('message')}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    time.sleep(pause)
    return body, False


def block_of(period):
    """Blocks are numbered 1..96 from the start time; RTM drops the spaces."""
    hour, minute = (int(part) for part in period.split("-")[0].strip().split(":"))
    if minute % 15 or not 0 <= hour < 24:
        raise ValueError(f"unexpected period {period!r}")
    return hour * 4 + minute // 15 + 1


def normalise(market, body):
    price_key = MARKETS[market]["price"]
    rows = []
    for row in json.loads(body)["data"]:
        day = datetime.strptime(row["date"], "%d-%m-%Y" if "-" in row["date"][:3] else "%Y-%m-%d").date()
        price = row.get(price_key)
        record = {
            "market": market,
            "delivery_date": day,
            "block": block_of(row["period"]),
            "price": None if price in (None, "") else float(price),
            "congestion": row.get("congestion") == "YES",
        }
        for name in VOLUMES:
            if name in row:
                record[name.replace("total_", "")] = None if row[name] in (None, "") else float(row[name])
        rows.append(record)
    return rows


def audit(frame):
    """Flag every row the modelling code must not treat as an ordinary price."""
    frame = frame.sort_values(["market", "delivery_date", "block"]).reset_index(drop=True)
    counts = frame.groupby(["market", "delivery_date"])["block"].transform("count")
    frame["complete_day"] = counts == BLOCKS
    frame["cap"] = [cap_for(day, market) for day, market in zip(frame["delivery_date"], frame["market"], strict=True)]
    frame["at_cap"] = frame["price"] >= frame["cap"] - 0.01
    volume = frame["mcv"] if "mcv" in frame else pd.Series(index=frame.index, dtype=float)
    # A zero price with zero cleared volume is a suspended session, not a price.
    frame["suspended"] = frame["price"].eq(0) & volume.fillna(0).eq(0)
    frame["usable"] = frame["price"].notna() & ~frame["suspended"] & frame["complete_day"]
    return frame


def build(market, first, last, pause=1.0):
    rows, sources, cached = [], [], 0
    for start, end in months(first, last):
        body, hit = fetch(market, start, end, pause)
        cached += hit
        rows.extend(normalise(market, body))
        sources.append({
            "market": market, "from": start.isoformat(), "to": end.isoformat(),
            "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
        })
    if not rows:
        raise RuntimeError(f"{market}: no rows for {first}..{last}")
    frame = audit(pd.DataFrame(rows))
    duplicated = frame.duplicated(["market", "delivery_date", "block"])
    if duplicated.any():
        raise ValueError(f"{market}: {int(duplicated.sum())} duplicate blocks")
    return frame, sources, cached


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets", default="dam,rtm,gdam,hpdam")
    parser.add_argument("--from", dest="first", type=date.fromisoformat, default=date(2022, 1, 1))
    parser.add_argument("--to", dest="last", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument("--pause", type=float, default=1.0)
    args = parser.parse_args()

    ROOT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for market in args.markets.split(","):
        first = max(args.first, MARKETS[market]["start"])
        frame, sources, cached = build(market, first, args.last, args.pause)
        path = ROOT / f"{market}_15m.parquet"
        frame.to_parquet(path, index=False)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        days = frame["delivery_date"].nunique()
        manifest.append({
            "market": market, "rows": len(frame), "days": days,
            "complete_days": int(frame.groupby("delivery_date")["complete_day"].first().sum()),
            "usable_rows": int(frame["usable"].sum()), "at_cap_rows": int(frame["at_cap"].sum()),
            "first": str(frame["delivery_date"].min()), "last": str(frame["delivery_date"].max()),
            "parquet_sha256": digest, "requests": sources,
        })
        print(f"{market:6} {len(frame):>8,} rows  {days:>5} days  "
              f"{int(frame['usable'].sum()):>8,} usable  {int(frame['at_cap'].sum()):>6,} at cap  "
              f"({len(sources)} requests, {cached} cached)")
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
