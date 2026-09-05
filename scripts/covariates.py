"""weather covariates for the benchmark, in two flavours.

the distinction is the whole point of the evaluation:

  actuals   what the weather turned out to be. you only ever know this after
            the fact, so a forecast built on it is measuring a ceiling.
  forecast  what the weather forecast said on the day the demand forecast was
            made, at the correct lead time. this is what you would really have.

both come from open-meteo and are aggregated from hourly to daily the same
way, so the only difference between the two runs of the benchmark is
foresight, not the dataset.
"""

import json
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

CACHE = Path(__file__).resolve().parent.parent / "tmp" / "weather-cache"
CACHE.mkdir(parents=True, exist_ok=True)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

# the previous runs api only carries lead times back about a week, which is
# why the headline benchmark forecasts 7 days rather than 14
MAX_LEAD = 7

# a day needs most of its hours before it can be aggregated into a daily value
MIN_HOURS_PER_DAY = 20

HOURLY = ["temperature_2m", "shortwave_radiation", "wind_speed_100m"]

# the previous runs archive only carries temperature for these locations.
# radiation and wind come back completely empty, so a lead-time-correct
# comparison can only be made on temperature. holding the variable set fixed
# across the actual and forecast runs is what keeps the comparison honest.
HOURLY_FORECAST = ["temperature_2m"]
TEMP_FEATURES = ["temp_mean", "temp_max"]

# state capital. a point rather than a state average, which is cruder than the
# era5 state files, but it is the same point for actuals and forecasts so the
# comparison between them stays clean.
COORDS = {
    "Karnataka": (12.97, 77.59),
    "Gujarat": (23.03, 72.58),
    "Delhi": (28.61, 77.21),
    "Maharashtra": (19.08, 72.88),
    "Tamil Nadu": (13.08, 80.27),
    "Assam": (26.14, 91.74),
}

FEATURES = ["temp_mean", "temp_max", "ghi", "wind"]


def _get(url: str, params: dict, cache_key: str) -> dict:
    path = CACHE / f"{cache_key}.json"
    if path.exists():
        return json.loads(path.read_text())
    for attempt in range(4):
        r = requests.get(url, params=params, timeout=120)
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise RuntimeError(body.get("reason"))
        path.write_text(json.dumps(body))
        return body
    raise RuntimeError(f"rate limited by {url}")


def _to_daily(frame: pd.DataFrame, suffix: str = "", temp_only: bool = False) -> pd.DataFrame:
    """hourly -> daily features.

    the coverage check is not paranoia. the previous runs archive returns rows
    for every hour but leaves most variables null, and a plain resample().sum()
    treats those nulls as zero. that silently produced a 76% low bias in
    radiation that looked like a real result rather than an empty column.
    """
    # a missing hour comes back as null, which makes pandas read the whole
    # column as objects and refuse to aggregate it
    frame = frame.apply(pd.to_numeric, errors="coerce")

    wanted = HOURLY_FORECAST if temp_only else HOURLY

    # coverage has to be checked per day, not overall. delhi's lead 7 archive
    # is 93% populated in total but january 2024 is only 21%, and january is
    # its coldest month. interpolating across that gap manufactured a +5.8C
    # warm bias that looked exactly like a real forecast failure.
    enough = None
    for name in wanted:
        hours = frame[f"{name}{suffix}"].notna().resample("D").sum()
        ok = hours >= MIN_HOURS_PER_DAY
        enough = ok if enough is None else (enough & ok)

    t = frame[f"temperature_2m{suffix}"]
    daily = {"temp_mean": t.resample("D").mean(), "temp_max": t.resample("D").max()}
    if not temp_only:
        daily["ghi"] = frame[f"shortwave_radiation{suffix}"].resample("D").sum() / 1000
        daily["wind"] = frame[f"wind_speed_100m{suffix}"].resample("D").max()

    # a thin day is missing, not zero and not the average of its neighbours
    return pd.DataFrame(daily).where(enough)


def actuals(state: str, start: date, end: date) -> pd.DataFrame:
    """what the weather turned out to be, daily, indexed by date"""
    lat, lon = COORDS[state]
    body = _get(
        ARCHIVE_URL,
        {
            "latitude": lat,
            "longitude": lon,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": ",".join(HOURLY),
            "timezone": "UTC",
        },
        f"actual_{state.replace(' ', '_')}_{start}_{end}",
    )
    hourly = pd.DataFrame(body["hourly"])
    hourly["time"] = pd.to_datetime(hourly["time"])
    return _to_daily(hourly.set_index("time"))


def forecast_by_lead(state: str, start: date, end: date) -> dict[int, pd.DataFrame]:
    """what the forecast said, keyed by lead time in days.

    lead N means: the value predicted for that day, by the model run N days
    earlier. so a demand forecast made on day zero uses lead 1 for tomorrow,
    lead 2 for the day after, and so on.
    """
    lat, lon = COORDS[state]
    out: dict[int, pd.DataFrame] = {}
    for lead in range(1, MAX_LEAD + 1):
        variables = [f"{v}_previous_day{lead}" for v in HOURLY_FORECAST]
        body = _get(
            PREVIOUS_RUNS_URL,
            {
                "latitude": lat,
                "longitude": lon,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "hourly": ",".join(variables),
                "timezone": "UTC",
            },
            f"lead{lead}_{state.replace(' ', '_')}_{start}_{end}",
        )
        hourly = pd.DataFrame(body["hourly"])
        hourly["time"] = pd.to_datetime(hourly["time"])
        out[lead] = _to_daily(
            hourly.set_index("time"), suffix=f"_previous_day{lead}", temp_only=True
        )
    return out


def horizon_covariates(
    leads: dict[int, pd.DataFrame], origin: pd.Timestamp, horizon: int
) -> pd.DataFrame:
    """the diagonal: day origin+n taken from the forecast at lead n.

    this is what a forecaster actually holds on the morning of the origin.
    """
    rows = []
    for n in range(1, horizon + 1):
        target = origin + timedelta(days=n)
        lead = leads[min(n, MAX_LEAD)]
        if target not in lead.index or lead.loc[target].isna().any():
            # no real forecast for this day. skip the origin rather than invent one.
            return pd.DataFrame()
        rows.append(lead.loc[target])
    return pd.DataFrame(rows)


def rolling_bias(
    actual: pd.DataFrame, leads: dict[int, pd.DataFrame], window: int = 60
) -> dict[int, pd.DataFrame]:
    """trailing mean of (forecast - observed) per lead, usable without leakage.

    the forecast archive and the reanalysis archive are different models on
    different grids, so they disagree systematically before forecast skill
    enters into it. delhi's day-ahead temperature sits about 4C above the
    reanalysis, which is far too large to be forecast error and would
    otherwise be scored as one.

    correcting against recent observations is what operational forecasters do,
    and shifting by a day means every correction uses only what was already
    known when the forecast was made.
    """
    out = {}
    for lead, frame in leads.items():
        diff = frame[TEMP_FEATURES] - actual[TEMP_FEATURES]
        out[lead] = diff.rolling(window, min_periods=20).mean().shift(1)
    return out


def horizon_covariates_corrected(
    leads: dict[int, pd.DataFrame],
    bias: dict[int, pd.DataFrame],
    origin: pd.Timestamp,
    horizon: int,
) -> pd.DataFrame:
    """the lead-correct diagonal, with each lead's known bias removed"""
    rows = []
    for n in range(1, horizon + 1):
        target = origin + timedelta(days=n)
        lead_n = min(n, MAX_LEAD)
        frame, b = leads[lead_n], bias[lead_n]
        if target not in frame.index or frame.loc[target].isna().any():
            return pd.DataFrame()
        if origin not in b.index or b.loc[origin].isna().any():
            return pd.DataFrame()
        rows.append(frame.loc[target] - b.loc[origin])
    return pd.DataFrame(rows)
