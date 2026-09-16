"""Forecast the grid drivers two days out, so the price model can use them.

At the 09:30 cutoff on D-1 the newest PSP report covers D-2, so a driver value
for the delivery day is two days away, not one. Every forecast here is a single
192-block run from that cutoff: the first 96 blocks are D-1, the last 96 are D.
Both halves come from one information set, which is what makes them safe to hand
to the price model as covariates.

Forecasts are computed once and stored. The price backtest then only looks them
up, exactly as it does with weather, so no model can accidentally re-run this
with a different cutoff.
"""

import argparse
import time
from datetime import date

import numpy as np
import pandas as pd

from iex.backtest import BLOCKS
from iex.drivers import STORE, DriverHistory, load, usable_days
from iex.timesfm import MAX_CONTEXT, session

FIELDS = ("demand", "wind", "solar", "net_demand")
LEADS = (1, 2)
HORIZON = len(LEADS) * BLOCKS


def forecast_day(model, history, fields=FIELDS, context_days=90):
    """One 192-block run per driver, all issued at the same cutoff."""
    context_blocks = min(context_days * BLOCKS, MAX_CONTEXT)
    rows = []
    for field in fields:
        series = history.series(field).to_numpy().reshape(-1)
        series = series[~np.isnan(series)]
        if len(series) < 8 * BLOCKS:
            return None
        output = model.predict(series[-context_blocks:], horizon=HORIZON,
                               return_quantiles=False, make_positive=True)
        values = np.asarray(output.forecast, dtype=float).reshape(len(LEADS), BLOCKS)
        for position, lead in enumerate(LEADS):
            rows.append(pd.DataFrame({
                "delivery_date": history.delivery_date, "lead": lead,
                "block": range(1, BLOCKS + 1), "field": field, "forecast": values[position],
            }))
    return pd.concat(rows, ignore_index=True)


def build(first, last, context_days=90, fields=FIELDS, limit=None):
    table = load()
    usable = usable_days(table)
    model, device = session()
    days = [d for d in pd.date_range(first, last, freq="D") if usable.get(d, False)]
    if limit:
        days = days[:limit]

    started, pieces, skipped = time.perf_counter(), [], []
    for day in days:
        frame = forecast_day(model, DriverHistory(table, day, usable), fields, context_days)
        if frame is None:
            skipped.append(day)
            continue
        pieces.append(frame)
    if not pieces:
        raise RuntimeError(f"no forecastable days between {first:%Y-%m-%d} and {last:%Y-%m-%d}")

    out = pd.concat(pieces, ignore_index=True)
    STORE.mkdir(parents=True, exist_ok=True)
    path = STORE / "forecast.parquet"
    out.to_parquet(path, index=False)
    return out, skipped, path, device, time.perf_counter() - started


def load_forecasts():
    path = STORE / "forecast.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run python -m iex.driverforecast")
    return pd.read_parquet(path)


def score(forecasts, actuals, fields=FIELDS):
    """Driver forecast error, split by how far ahead the day was."""
    truth = actuals.reset_index().melt(id_vars=["delivery_date", "block"],
                                       var_name="field", value_name="actual")
    rows = []
    for lead in LEADS:
        window = forecasts[forecasts["lead"] == lead].copy()
        # A lead of 1 is a forecast of the day before delivery, so it is scored
        # against that day rather than against the delivery day.
        window["day"] = window["delivery_date"] - pd.Timedelta(days=len(LEADS) - lead)
        merged = window.merge(truth.rename(columns={"delivery_date": "day"}),
                              on=["day", "block", "field"], how="inner")
        for field in fields:
            part = merged[merged["field"] == field]
            if part.empty:
                continue
            error = part["forecast"] - part["actual"]
            rows.append({
                "field": field, "lead_days": lead, "days": part["day"].nunique(),
                "mae": float(error.abs().mean()), "rmse": float(np.sqrt((error ** 2).mean())),
                "bias": float(error.mean()), "mean_level": float(part["actual"].mean()),
                "nmae": float(error.abs().mean() / max(part["actual"].mean(), 1) * 100),
            })
    return pd.DataFrame(rows)


def persistence(actuals, fields=FIELDS):
    """The same score for copying the last day that was known at the cutoff."""
    wide = {f: actuals[f].unstack("block") for f in fields if f in actuals.columns}
    rows = []
    for field, frame in wide.items():
        for lead in LEADS:
            # Forecasting day X from the last known day, which is two days before
            # the delivery day the cutoff belongs to.
            offset = len(LEADS) - lead + 2
            guess = frame.shift(offset).dropna()
            truth = frame.loc[guess.index].to_numpy().reshape(-1)
            error = guess.to_numpy().reshape(-1) - truth
            rows.append({
                "field": field, "lead_days": lead, "days": len(guess),
                "mae": float(np.abs(error).mean()), "rmse": float(np.sqrt((error ** 2).mean())),
                "bias": float(error.mean()), "mean_level": float(truth.mean()),
                "nmae": float(np.abs(error).mean() / max(truth.mean(), 1) * 100),
            })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="first", type=date.fromisoformat, default=date(2025, 2, 1))
    parser.add_argument("--to", dest="last", type=date.fromisoformat, default=date(2026, 9, 15))
    parser.add_argument("--context-days", type=int, default=90)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    forecasts, skipped, path, device, elapsed = build(
        pd.Timestamp(args.first), pd.Timestamp(args.last), args.context_days, limit=args.limit)
    days = forecasts["delivery_date"].nunique()
    print(f"{days} delivery days forecast on {device}, {len(skipped)} skipped, {elapsed / 60:.1f} min\n")

    actuals = load()
    ours = score(forecasts, actuals)
    theirs = persistence(actuals)
    board = ours.merge(theirs, on=["field", "lead_days"], suffixes=("", "_persist"))
    board["beats_persistence"] = (1 - board["mae"] / board["mae_persist"]) * 100

    print("driver forecast error in MW, against copying the last known day")
    print(board[["field", "lead_days", "days", "mean_level", "mae", "rmse", "bias", "nmae",
                 "mae_persist", "beats_persistence"]].round(1).to_string(index=False))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()


class Drivers:
    """Driver covariates for the price model, spanning context plus the horizon.

    Days up to D-2 are actuals, because the PSP report for them was published
    before the cutoff. D-1 and D come from the stored two-day forecast. Values are
    converted to GW so they sit in the same range as the other covariates.

    `actual=True` swaps in observed drivers for D-1 and D, which no forecaster
    could have had. It exists only to measure a ceiling and its results are
    always labelled as one.
    """

    def __init__(self, fields=("demand", "wind", "solar"), actual=False):
        self.fields = tuple(fields)
        self.actual = actual
        self.actuals = load()
        self.usable = usable_days(self.actuals)
        self.forecasts = None if actual else load_forecasts()
        missing = set(self.fields) - set(self.actuals.columns)
        if missing:
            raise ValueError(f"drivers carry no {sorted(missing)}")

    def _settled(self, history, days):
        """Published driver days, on the same gap-free grid the forecaster saw.

        Gaps are filled by DriverHistory, which only ever looks at days at or
        before the cutoff, so a missing day is never filled from a later one.
        """
        view = DriverHistory(self.actuals, history.delivery_date, self.usable)
        wanted = pd.DatetimeIndex(days).normalize()
        values = {}
        for field in self.fields:
            frame = view.series(field).reindex(wanted)
            if frame.isna().any().any():
                gaps = frame[frame.isna().any(axis=1)].index
                raise ValueError(f"drivers missing for {len(gaps)} days, first {gaps[0].date()}")
            values[field] = frame.to_numpy().reshape(-1)
        return values

    def _observed(self, days):
        """Actual drivers for the two days the cutoff cannot know, the ceiling only.

        Everything before them is shared with the honest run, so the two differ
        in exactly one thing: whether D-1 and D were known in advance.
        """
        wanted = pd.MultiIndex.from_product(
            [pd.DatetimeIndex(days).normalize(), range(1, BLOCKS + 1)],
            names=["delivery_date", "block"])
        frame = self.actuals.reindex(wanted)
        unusable = [d for d in pd.DatetimeIndex(days).normalize() if not self.usable.get(d, False)]
        if frame[list(self.fields)].isna().any().any() or unusable:
            raise ValueError(f"observed drivers unavailable for {pd.DatetimeIndex(days)[0].date()}")
        return {field: frame[field].to_numpy() for field in self.fields}

    def scorable(self, days):
        """The delivery days this provider can actually serve, forecast or ceiling."""
        if self.actual:
            return {d for d in days
                    if all(self.usable.get(pd.Timestamp(d) - pd.Timedelta(days=k), False)
                           for k in (0, 1))}
        issued = set(self.forecasts["delivery_date"].unique())
        return {d for d in days if pd.Timestamp(d) in issued}

    def _predicted(self, delivery_date):
        window = self.forecasts[self.forecasts["delivery_date"] == delivery_date]
        if window.empty:
            raise ValueError(f"no driver forecast issued for {delivery_date.date()}")
        values = {}
        for field in self.fields:
            part = window[window["field"] == field].sort_values(["lead", "block"])
            if len(part) != HORIZON:
                raise ValueError(f"{field} forecast for {delivery_date.date()} has {len(part)} blocks")
            values[field] = part["forecast"].to_numpy()
        return values

    def __call__(self, history, context_days):
        days = list(history.prices().index[-context_days:]) + [history.delivery_date]
        # The last two days of the span are D-1 and D, neither of which the cutoff
        # knows; everything before them is published fact.
        settled = self._settled(history, days[:-2])
        ahead = self._observed(days[-2:]) if self.actual else self._predicted(history.delivery_date)
        return {f"driver_{field}": np.concatenate([settled[field], ahead[field]]) / 1000
                for field in self.fields}
