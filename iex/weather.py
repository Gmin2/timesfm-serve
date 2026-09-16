"""Weather forecasts as TimesFM covariates, restricted to what existed at the cutoff.

Only forecasts issued before 09:30 IST on the day before delivery may be used, so
this reads Open-Meteo's `_previous_day2` fields: the run issued two days before the
valid time. The `_previous_day1` fields are issued the day before and would include
runs published after the cutoff, so they are never used.

Actual observed weather is fetched separately and used only to measure a ceiling,
never as an input to a reported forecast.
"""

import argparse
import json
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests

from iex.backtest import BLOCKS

STORE = Path("data/iex/weather")
FORECAST_API = "https://previous-runs-api.open-meteo.com/v1/forecast"
ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"
MODEL = "ecmwf_ifs025"
# Major load centres, weighted roughly by regional share of national demand.
CITIES = {
    "delhi": (28.61, 77.21, 0.18),
    "mumbai": (19.08, 72.88, 0.16),
    "bengaluru": (12.97, 77.59, 0.13),
    "chennai": (13.08, 80.27, 0.12),
    "kolkata": (22.57, 88.36, 0.11),
    "hyderabad": (17.39, 78.49, 0.11),
    "ahmedabad": (23.03, 72.59, 0.10),
    "lucknow": (26.85, 80.95, 0.09),
}
VARIABLES = ("temperature_2m", "shortwave_radiation")
# Wind generation happens where the wind is, not where the demand is. Regional
# shares are measured from 678 days of Grid-India's own regional wind generation
# (WR 46.9%, SR 44.7%, NR 8.4%, and nothing at all in the east); within a region
# the split follows state installed capacity.
WIND_SITES = {
    "kutch": (23.25, 69.67, 0.281),
    "muppandal": (8.25, 77.55, 0.227),
    "chitradurga": (14.22, 76.40, 0.135),
    "satara": (17.30, 74.20, 0.120),
    "anantapur": (14.68, 77.60, 0.085),
    "jaisalmer": (26.91, 70.92, 0.084),
    "dewas": (22.97, 76.06, 0.068),
}
WIND_VARIABLES = ("wind_speed_100m",)
# Turbine power goes as the cube of wind speed, so the cube is what carries the
# signal. Cubing happens per site before weighting, because the mean of cubes and
# the cube of the mean are not the same thing across a spread-out fleet.
GROUPS = {
    "demand": (CITIES, VARIABLES, False),
    "wind": (WIND_SITES, WIND_VARIABLES, True),
}


def fetch(city, first, last, actual=False, group="demand"):
    """One site, one date range, cached on disk as the raw response."""
    sites, variables, _ = GROUPS[group]
    latitude, longitude, _ = sites[city]
    kind = "actual" if actual else "forecast"
    # The demand group keeps its original filenames so the frozen headline never
    # re-downloads and never silently moves if the archive is revised.
    stem = city if group == "demand" else f"{group}_{city}"
    path = STORE / kind / f"{stem}_{first:%Y%m%d}_{last:%Y%m%d}.json"
    if path.exists():
        return json.loads(path.read_text())
    fields = variables if actual else tuple(f"{v}_previous_day2" for v in variables)
    query = urlencode({
        "latitude": latitude, "longitude": longitude,
        "start_date": first.isoformat(), "end_date": last.isoformat(),
        "hourly": ",".join(fields), "timezone": "Asia/Kolkata",
        **({} if actual else {"models": MODEL}),
    })
    url = f"{ARCHIVE_API if actual else FORECAST_API}?{query}"
    response = requests.get(url, headers={"User-Agent": "iex-research"}, timeout=180)
    response.raise_for_status()
    payload = response.json()
    if "hourly" not in payload:
        raise RuntimeError(f"{city} {first}..{last}: {payload}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    time.sleep(1)
    return payload


def national(first, last, actual=False, group="demand"):
    """One weighted national series per variable, hourly, IST."""
    sites, variables, cube = GROUPS[group]
    frames = []
    for city, (_, _, weight) in sites.items():
        hourly = fetch(city, first, last, actual, group)["hourly"]
        suffix = "" if actual else "_previous_day2"
        frame = pd.DataFrame({
            "time": pd.to_datetime(hourly["time"]),
            **{v: pd.Series(hourly[f"{v}{suffix}"], dtype="float64") for v in variables},
        }).set_index("time")
        frames.append((frame ** 3 if cube else frame) * weight)
    total = sum(weight for _, _, weight in sites.values())
    return sum(frames) / total


def to_blocks(hourly, variables=VARIABLES):
    """Hourly values repeated to 15-minute blocks, indexed by (date, block)."""
    repeated = hourly.reindex(hourly.index.repeat(BLOCKS // 24))
    repeated["delivery_date"] = repeated.index.normalize()
    repeated["block"] = np.tile(np.arange(1, BLOCKS + 1), len(hourly) // 24)
    return repeated.set_index(["delivery_date", "block"])[list(variables)]


def store_path(actual=False, group="demand"):
    stem = "actual" if actual else "forecast"
    return STORE / (f"{stem}.parquet" if group == "demand" else f"{group}_{stem}.parquet")


def build(first, last, actual=False, group="demand"):
    _, variables, _ = GROUPS[group]
    table = to_blocks(national(first, last, actual, group), variables)
    STORE.mkdir(parents=True, exist_ok=True)
    path = store_path(actual, group)
    table.to_parquet(path)
    return table, path


def load(actual=False, group="demand"):
    path = store_path(actual, group)
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run python -m iex.weather --from ... --to ...")
    return pd.read_parquet(path)


class Weather:
    """Future covariates from the weather table, spanning context plus horizon.

    `actual=True` deliberately uses observed weather, which no real forecaster
    could have. It exists only to measure how much accuracy imperfect weather
    forecasts cost, and its results are always labelled as a ceiling.
    """

    def __init__(self, actual=False):
        self.table = load(actual)
        self.actual = actual

    def __call__(self, history, context_days):
        days = list(history.prices().index[-context_days:]) + [history.delivery_date]
        wanted = pd.MultiIndex.from_product(
            [pd.DatetimeIndex(days).normalize(), range(1, BLOCKS + 1)],
            names=["delivery_date", "block"])
        frame = self.table.reindex(wanted)
        if frame.isna().any().any():
            missing = frame[frame.isna().any(axis=1)].index.get_level_values(0).unique()
            raise ValueError(f"weather missing for {len(missing)} days, first {missing[0].date()}")
        return {name: frame[name].to_numpy() for name in VARIABLES}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="first", type=date.fromisoformat, default=date(2024, 4, 1))
    parser.add_argument("--to", dest="last", type=date.fromisoformat, default=date.today() - timedelta(days=2))
    parser.add_argument("--actual", action="store_true", help="fetch observed weather for the ceiling run")
    parser.add_argument("--group", default="demand", choices=sorted(GROUPS),
                        help="which set of sites and variables to pull")
    args = parser.parse_args()

    table, path = build(args.first, args.last, args.actual, args.group)
    kind = "observed" if args.actual else "forecast issued two days ahead"
    days = table.index.get_level_values(0).nunique()
    print(f"{kind}: {len(table):,} blocks over {days} days, {args.first} to {args.last}")
    print(table.describe().round(2).to_string())
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
