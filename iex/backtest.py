"""Rolling day-ahead backtest with a hard information cutoff.

Bidding for delivery day D opens at 10:00 IST on D-1, so every forecast here is
issued at 09:30 on D-1. A forecaster is handed a History object and can only read
through it; History refuses to return anything at or after the cutoff, so leakage
is a raised exception rather than a silently better score.
"""

import numpy as np
import pandas as pd

BLOCKS = 96
QUANTILE_LEVELS = tuple(level / 10 for level in range(1, 10))
# Evaluation periods are fixed before any model runs. Tuning happens on
# development only; test is scored once, and the September 2026 scarcity regime
# is reported separately rather than tuned on.
PERIODS = {
    "development": (pd.Timestamp("2024-04-01"), pd.Timestamp("2025-03-31")),
    "test": (pd.Timestamp("2025-04-01"), pd.Timestamp("2026-08-31")),
    "stress": (pd.Timestamp("2026-09-01"), pd.Timestamp("2026-09-15")),
}


class LeakageError(AssertionError):
    pass


class History:
    """Read-only view of the market that stops at the forecast's cutoff."""

    def __init__(self, table, delivery_date):
        self.delivery_date = pd.Timestamp(delivery_date)
        self.cutoff = self.delivery_date - pd.Timedelta(days=1)
        self._table = table

    def _upto_cutoff(self, market):
        frame = self._table[self._table["market"] == market]
        return frame[frame["delivery_date"] <= self.cutoff]

    def prices(self, market="dam", days=None):
        """Daily price series, oldest first, one row per delivery day."""
        wide = self._upto_cutoff(market).pivot_table(index="delivery_date", columns="block", values="price")
        wide = wide.reindex(columns=range(1, BLOCKS + 1))
        if wide.index.max() > self.cutoff:
            raise LeakageError(f"history reaches {wide.index.max()}, past cutoff {self.cutoff}")
        return wide.tail(days) if days else wide

    def column(self, name, market="dam", days=None):
        frame = self._table[(self._table["market"] == market) & (self._table["delivery_date"] <= self.cutoff)]
        wide = frame.pivot_table(index="delivery_date", columns="block", values=name)
        return wide.tail(days) if days else wide

    def last_day(self, market="dam"):
        return self.prices(market, days=1).to_numpy()[0]

    def same_weekday_last_week(self, market="dam"):
        return self.prices(market, days=7).to_numpy()[0]


def naive_yesterday(history):
    return history.last_day()


def naive_last_week(history):
    return history.same_weekday_last_week()


def climatology(history, days=28):
    return history.prices(days=days).to_numpy().mean(axis=0)


def run(table, forecasters, period="development", markets=("dam",), cap=10_000.0, limit=None,
        first_day=None):
    """Replay every delivery day in a period, one forecast per day per model.

    `first_day` starts later than the period normally would, for comparisons whose
    inputs do not reach back far enough. Every model is scored on the same days.
    """
    first, last = PERIODS[period]
    if first_day is not None:
        first = max(first, pd.Timestamp(first_day))
    table = table[table["market"].isin(markets)]
    days = sorted(d for d in table["delivery_date"].unique() if first <= pd.Timestamp(d) <= last)
    if limit:
        days = days[:limit]
    truth = table[table["market"] == "dam"].pivot_table(index="delivery_date", columns="block", values="price")
    usable = table[table["market"] == "dam"].groupby("delivery_date")["usable"].all()

    rows = []
    for day in days:
        if not usable.get(day, False):
            continue
        history = History(table, day)
        if len(history.prices(days=8)) < 8:
            continue
        actual = truth.loc[day].to_numpy()
        for name, forecaster in forecasters.items():
            forecast = np.clip(np.asarray(forecaster(history), dtype=float), 0, cap)
            if forecast.shape != (BLOCKS,):
                raise ValueError(f"{name} returned {forecast.shape}, expected ({BLOCKS},)")
            frame = pd.DataFrame({
                "delivery_date": pd.Timestamp(day), "block": range(1, BLOCKS + 1),
                "model": name, "forecast": forecast, "actual": actual,
                "at_cap": actual >= cap - 0.01,
            })
            # Models that expose quantiles get them recorded, for calibration later.
            quantiles = getattr(forecaster, "quantiles", None)
            if quantiles is not None and np.shape(quantiles) == (BLOCKS, len(QUANTILE_LEVELS)):
                for position, level in enumerate(QUANTILE_LEVELS):
                    frame[f"q{int(level * 100)}"] = np.clip(np.asarray(quantiles)[:, position], 0, cap)
            rows.append(frame)
    if not rows:
        raise RuntimeError(f"no scorable days in {period}")
    return pd.concat(rows, ignore_index=True)


def score(results, reference="naive_yesterday"):
    """MAE overall and on capped blocks, plus error relative to a reference model."""
    results = results.assign(error=(results["forecast"] - results["actual"]).abs())
    table = results.groupby("model").agg(
        days=("delivery_date", "nunique"),
        blocks=("error", "size"),
        mae=("error", "mean"),
        rmse=("error", lambda e: float(np.sqrt((e**2).mean()))),
        bias=("model", lambda _: np.nan),
    )
    table["bias"] = results.groupby("model").apply(
        lambda f: float((f["forecast"] - f["actual"]).mean()), include_groups=False)
    capped = results[results["at_cap"]]
    table["mae_at_cap"] = capped.groupby("model")["error"].mean()
    table["mae_normal"] = results[~results["at_cap"]].groupby("model")["error"].mean()
    if reference in table.index:
        table["rmae"] = table["mae"] / table.loc[reference, "mae"]
    return table.sort_values("mae")


def daily_losses(results):
    """Mean absolute error per day per model, the unit the significance test uses."""
    losses = results.assign(error=(results["forecast"] - results["actual"]).abs())
    return losses.pivot_table(index="delivery_date", columns="model", values="error", aggfunc="mean")


def diebold_mariano(losses, model, reference):
    """Two-sided test on the daily loss differential, with a HAC variance.

    epftoolbox's own implementation loops over 24 hours and silently returns 0 or 1
    for later blocks, so this is written here rather than imported.
    """
    from scipy import stats

    difference = (losses[model] - losses[reference]).dropna().to_numpy()
    n = len(difference)
    if n < 30:
        raise ValueError(f"only {n} paired days, too few to test")
    mean = difference.mean()
    lag = int(np.floor(1.5 * n ** (1 / 3)))
    variance = difference.var(ddof=1)
    for k in range(1, lag + 1):
        covariance = np.cov(difference[k:], difference[:-k], ddof=1)[0, 1]
        variance += 2 * (1 - k / (lag + 1)) * covariance
    statistic = mean / np.sqrt(max(variance, 1e-12) / n)
    return {
        "model": model, "reference": reference, "days": n,
        "mean_daily_difference": float(mean),
        "statistic": float(statistic),
        "p_value": float(2 * (1 - stats.norm.cdf(abs(statistic)))),
        "better": bool(mean < 0),
    }


BASELINES = {
    "naive_yesterday": naive_yesterday,
    "naive_last_week": naive_last_week,
    "climatology_28d": climatology,
}


def load(market="dam"):
    table = pd.read_parquet(f"data/iex/{market}_15m.parquet")
    table["delivery_date"] = pd.to_datetime(table["delivery_date"])
    return table


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="development", choices=sorted(PERIODS))
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    first, last = PERIODS[args.period]
    results = run(load(), BASELINES, period=args.period, limit=args.limit)
    board = score(results)
    print(f"{args.period}: {first.date()} to {last.date()}, {int(board['days'].iloc[0])} days scored\n")
    print(board[["days", "mae", "rmse", "bias", "mae_normal", "mae_at_cap", "rmae"]].round(1).to_string())
    losses = daily_losses(results)
    print("\nDiebold-Mariano against naive_yesterday:")
    for model in board.index:
        if model == "naive_yesterday":
            continue
        verdict = diebold_mariano(losses, model, "naive_yesterday")
        print(f"  {model:18} {'better' if verdict['better'] else 'worse':6}  p = {verdict['p_value']:.4f}")


if __name__ == "__main__":
    main()
