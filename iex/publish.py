"""Issue one day-ahead forecast and store it, the way the CronJob will.

Bidding for delivery day D opens at 10:00 IST on D-1, so this runs at 09:30 and
builds its forecast through the same History object the backtest uses. The model
cannot see anything the backtest would have refused it, because it is the same
code path; there is no separate production shortcut.
"""

import argparse
import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from iex import store
from iex.backtest import History, load
from iex.calibrate import COLUMNS, calibrate
from iex.timesfm import REVISION, TimesFM

IST = timezone(timedelta(hours=5, minutes=30))
# CERC caps the day-ahead price, so a forecast above it is not a forecast of
# anything that can happen. The backtest clips; so must anything we publish.
CAP = 10_000.0
CUTOFF_HOUR, CUTOFF_MINUTE = 9, 30
MODEL = "timesfm_90d_cal_drvwx"


def cutoff_for(delivery_date):
    """09:30 IST on the day before delivery, as an absolute instant."""
    previous = pd.Timestamp(delivery_date).date() - timedelta(days=1)
    return datetime.combine(previous, datetime.min.time(), IST).replace(
        hour=CUTOFF_HOUR, minute=CUTOFF_MINUTE)


def build_model():
    from iex.driverforecast import Drivers
    from iex.weather import Weather

    return TimesFM(context_days=90, log=True, use_calendar=True,
                   weather=Weather(), drivers=Drivers(variant="_wx"))


def issue(table, delivery_date, model=None, name=MODEL):
    """Forecast one delivery day, with the quantiles the calibration expects."""
    model = model or build_model()
    history = History(table, delivery_date)
    if history.prices(days=8).shape[0] < 8:
        raise RuntimeError(f"not enough price history before {delivery_date}")
    forecast = np.clip(np.asarray(model(history), dtype=float), 0, CAP)
    quantiles = getattr(model, "quantiles", None)
    if quantiles is not None:
        quantiles = np.clip(np.asarray(quantiles, dtype=float), 0, CAP)
    return {"delivery_date": pd.Timestamp(delivery_date).date(), "model": name,
            "cutoff_at": cutoff_for(delivery_date), "forecast": forecast,
            "quantiles": quantiles, "revision": REVISION}


def calibrated_quantiles(issued, results=None):
    """Apply the rolling conformal correction if a settled history is available."""
    if issued["quantiles"] is None or results is None or results.empty:
        return issued["quantiles"]
    rows = pd.DataFrame({
        "delivery_date": issued["delivery_date"], "block": range(1, len(issued["forecast"]) + 1),
        "model": issued["model"], "forecast": issued["forecast"],
        "actual": float("nan"), "at_cap": False,
    })
    for position, column in enumerate(COLUMNS):
        rows[column] = [q[position] for q in issued["quantiles"]]
    combined = pd.concat([results, rows], ignore_index=True)
    adjusted = calibrate(combined)
    today = adjusted[adjusted["delivery_date"] == pd.Timestamp(issued["delivery_date"])]
    return today[COLUMNS].to_numpy() if len(today) else issued["quantiles"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", type=date.fromisoformat,
                        help="delivery day to forecast; default is the day after the last price")
    parser.add_argument("--dry-run", action="store_true", help="print it instead of storing it")
    args = parser.parse_args()

    table = load()
    delivery = args.day or (table["delivery_date"].max() + pd.Timedelta(days=1)).date()
    issued = issue(table, pd.Timestamp(delivery))

    blocks = issued["forecast"]
    print(f"{issued['model']} for {delivery}, cutoff {issued['cutoff_at']:%Y-%m-%d %H:%M %Z}")
    print(f"96 blocks, mean Rs {blocks.mean():.0f}/MWh, "
          f"low Rs {blocks.min():.0f}, peak Rs {blocks.max():.0f}")
    if args.dry_run:
        print(json.dumps([round(float(v), 2) for v in blocks[:8]]) + " ...")
        return

    from timesfm_serve import db

    db.open_pool()
    try:
        with db.conn() as connection:
            identifier = store.record(connection, issued["delivery_date"], issued["model"],
                                      issued["cutoff_at"], blocks, issued["quantiles"],
                                      issued["revision"])
    finally:
        # A CronJob's exit is a health signal, so the pool is closed here rather
        # than left to the interpreter, which complains on the way down.
        db.pool.close()
    print(f"stored as {identifier}" if identifier
          else f"{delivery} was already issued; forecasts are never overwritten")


if __name__ == "__main__":
    main()
