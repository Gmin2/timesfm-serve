"""LEAR, the standard benchmark in electricity price forecasting.

One lasso per block, choosing from price lags at 1, 2, 3 and 7 days plus weekday
indicators, refitted for every delivery day. Follows Lago, Marcjasz, De Schutter
and Weron (2021): an asinh transform with median/MAD scaling, the penalty chosen
by LassoLarsIC, and an ensemble over several calibration windows.

Written here rather than taken from epftoolbox, whose feature builder is hardcoded
to 24 hours a day and whose significance tests silently mishandle 96 blocks.
"""

import numpy as np

from iex.backtest import BLOCKS

LAGS = (1, 2, 3, 7)
WINDOWS = (56, 84, 364, 728)
# Lago's design has 24 blocks a day; at 96 it would take 4x96 price features, more
# than the calibration windows have samples, which leaves the information criterion
# unable to estimate its own noise variance. Keeping yesterday at full resolution
# and reducing the older lags to hourly means restores samples > features on the
# long windows and cuts the count from 391 to 175.
HOURS = 24


def scaling(values):
    """Median and MAD per column, the robust scaling the paper specifies."""
    median = np.median(values, axis=0)
    spread = np.median(np.abs(values - median), axis=0) * 1.4826
    return median, np.where(spread == 0, 1.0, spread)


def transform(values, median, spread):
    return np.arcsinh((values - median) / spread)


def invert(values, median, spread):
    return np.sinh(values) * spread + median


def hourly(blocks):
    """Average 96 blocks down to 24 hourly means."""
    return blocks.reshape(HOURS, BLOCKS // HOURS).mean(axis=1)


def features(prices, weekdays, index):
    """Yesterday at full resolution, older lags hourly, plus a weekday indicator."""
    lagged = [prices[index - 1]] + [hourly(prices[index - lag]) for lag in LAGS[1:]]
    return np.concatenate([*lagged, np.eye(7)[weekdays[index]]])


def fit_window(prices, weekdays, target, window, seed_alpha=None):
    """Fit 96 lassos on the `window` days before `target` and forecast that day."""
    from sklearn.linear_model import Lasso, LassoLarsIC

    first = target - window
    if first - max(LAGS) < 0:
        raise ValueError(f"need {window + max(LAGS)} days of history, have {target}")
    rows = np.array([features(prices, weekdays, day) for day in range(first, target)])
    outcomes = prices[first:target]

    dummies = rows[:, -7:]
    numeric = rows[:, :-7]
    centre, spread = scaling(numeric)
    inputs = np.hstack([transform(numeric, centre, spread), dummies])
    out_centre, out_spread = scaling(outcomes)
    outputs = transform(outcomes, out_centre, out_spread)

    query = features(prices, weekdays, target)
    query = np.hstack([transform(query[:-7], centre, spread), query[-7:]])[None]

    # When samples still fall short of features, scikit-learn cannot estimate the
    # noise variance and demands one. A fixed guess miscalibrates the criterion
    # badly, so use the day-to-day variance of the target itself, which is what a
    # persistence forecast would leave behind.
    short = inputs.shape[0] <= inputs.shape[1]
    forecast = np.empty(BLOCKS)
    alphas = np.empty(BLOCKS)
    for block in range(BLOCKS):
        noise = float(np.var(np.diff(outputs[:, block])) / 2) if short else None
        alpha = seed_alpha[block] if seed_alpha is not None else LassoLarsIC(
            criterion="aic", max_iter=2500, noise_variance=noise).fit(inputs, outputs[:, block]).alpha_
        alphas[block] = alpha
        forecast[block] = Lasso(alpha=alpha, max_iter=50_000).fit(inputs, outputs[:, block]).predict(query)[0]
    return invert(forecast, out_centre, out_spread), alphas


class Lear:
    """Callable forecaster for the backtest harness.

    Refitting 96 lassos for four windows on every delivery day is slow, so the
    penalties are reselected every `reselect_every` days and reused in between,
    which is the usual practical compromise and is recorded in the results.
    """

    def __init__(self, windows=WINDOWS, reselect_every=30, market="dam"):
        self.windows = windows
        self.reselect_every = reselect_every
        self.market = market
        self._alphas = {}
        self._since = {}

    def __call__(self, history):
        frame = history.prices(self.market)
        prices = frame.to_numpy()
        weekdays = frame.index.dayofweek.to_numpy()
        target = len(prices)
        # The delivery day sits one step past the end of history, so append a
        # placeholder row; only its weekday is ever read.
        prices = np.vstack([prices, np.zeros(BLOCKS)])
        weekdays = np.append(weekdays, (frame.index[-1] + np.timedelta64(1, "D")).dayofweek)

        forecasts = []
        for window in self.windows:
            if target - window - max(LAGS) < 0:
                continue
            due = self._since.get(window, self.reselect_every) >= self.reselect_every
            seed = None if due else self._alphas.get(window)
            forecast, alphas = fit_window(prices, weekdays, target, window, seed_alpha=seed)
            if due:
                self._alphas[window] = alphas
                self._since[window] = 0
            self._since[window] = self._since.get(window, 0) + 1
            forecasts.append(forecast)
        if not forecasts:
            raise ValueError("not enough history for any calibration window")
        return np.mean(forecasts, axis=0)


def main():
    import argparse
    import time

    from iex.backtest import BASELINES, PERIODS, daily_losses, diebold_mariano, load, run, score

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="development", choices=sorted(PERIODS))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--windows", default="56,84,364")
    parser.add_argument("--reselect-every", type=int, default=30)
    parser.add_argument("--out", default="results/iex")
    args = parser.parse_args()

    from pathlib import Path

    windows = tuple(int(w) for w in args.windows.split(","))
    models = dict(BASELINES)
    models["lear"] = Lear(windows=windows, reselect_every=args.reselect_every)

    started = time.perf_counter()
    results = run(load(), models, period=args.period, limit=args.limit)
    elapsed = time.perf_counter() - started

    board = score(results)
    days = int(board["days"].iloc[0])
    print(f"{args.period}: {days} days, windows {windows}, penalties reselected every "
          f"{args.reselect_every} days, {elapsed / 60:.1f} min\n")
    print(board[["days", "mae", "rmse", "bias", "mae_normal", "mae_at_cap", "rmae"]].round(1).to_string())
    losses = daily_losses(results)
    print("\nDiebold-Mariano against naive_yesterday:")
    for model in board.index:
        if model == "naive_yesterday":
            continue
        verdict = diebold_mariano(losses, model, "naive_yesterday")
        print(f"  {model:18} {'better' if verdict['better'] else 'worse':6}  p = {verdict['p_value']:.4f}")

    directory = Path(args.out)
    directory.mkdir(parents=True, exist_ok=True)
    results.to_parquet(directory / f"{args.period}_lear.parquet", index=False)
    print(f"\nwrote {directory / f'{args.period}_lear.parquet'}")


if __name__ == "__main__":
    main()
