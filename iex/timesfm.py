"""TimesFM 3.0 as a zero-shot day-ahead price forecaster.

The 96 blocks of a delivery day are the next 96 steps of one continuous
15-minute series, so the model sees prices exactly as the market produces them.
Nothing is trained here; the pinned checkpoint is used as published.
"""

import functools
import os

import numpy as np

from iex.backtest import BLOCKS

MODEL = "google/timesfm-3.0-pytorch"
REVISION = "43046b85ec22d584a13f8098c2ed39c889e129c2"
# The forecaster misaligns covariates past its maximum context, and a context of
# 160 days is already far longer than any useful price memory here.
MAX_CONTEXT = 15_360


@functools.cache
def session(cache_dir=None):
    import timesfm
    import torch
    from huggingface_hub import snapshot_download

    device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
    path = snapshot_download(MODEL, revision=REVISION, cache_dir=cache_dir or os.environ.get("HF_HUB_CACHE"),
                             local_files_only=True)
    return timesfm.TimesFM3Forecaster.from_pretrained(path, device=device, local_files_only=True), device


class TimesFM:
    """Callable forecaster for the backtest harness.

    `log` fits the model on log1p prices and inverts afterwards. Day-ahead prices
    are strictly positive and heavily skewed by the cap, so the transform is worth
    measuring rather than assuming either way.
    """

    def __init__(self, context_days=28, log=False, symmetric=False, market="dam",
                 cache_dir=None, use_calendar=False, use_bids=False):
        self.context_blocks = min(context_days * BLOCKS, MAX_CONTEXT)
        self.context_days = min(context_days, MAX_CONTEXT // BLOCKS)
        self.log = log
        self.symmetric = symmetric
        self.market = market
        self.cache_dir = cache_dir
        self.use_calendar = use_calendar
        self.use_bids = use_bids
        self.quantiles = None

    def _covariates(self, history):
        """Future covariates span context plus horizon; past-only stop at the cutoff."""
        from iex.covariates import future_calendar, past_bid_ratio

        future = past = None
        if self.use_calendar:
            values = future_calendar(history, self.context_days)
            future = np.vstack([values[name] for name in sorted(values)])
            expected = self.context_blocks + BLOCKS
            if future.shape[1] != expected:
                raise ValueError(f"future covariates are {future.shape[1]} long, expected {expected}")
        if self.use_bids:
            ratio = past_bid_ratio(history, self.context_days, self.market)
            if ratio.shape[0] != self.context_blocks:
                raise ValueError(f"past covariates are {ratio.shape[0]} long, expected {self.context_blocks}")
            past = ratio[None, :]
        return future, past

    def __call__(self, history):
        model, _ = session(self.cache_dir)
        series = history.prices(self.market).to_numpy().reshape(-1)
        context = series[-self.context_blocks:]
        if self.log:
            context = np.log1p(context)
        future, past = self._covariates(history)
        output = model.predict(
            context, horizon=BLOCKS, return_quantiles=True,
            past_future_covariates=future, past_only_covariates=past,
            use_symmetric_averaging=self.symmetric, make_positive=not self.log,
        )
        forecast = np.asarray(output.forecast, dtype=float)
        quantiles = np.asarray(output.quantiles, dtype=float)
        if self.log:
            forecast, quantiles = np.expm1(forecast), np.expm1(quantiles)
        self.quantiles = quantiles
        return forecast


def main():
    import argparse
    import time
    from pathlib import Path

    from iex.backtest import BASELINES, PERIODS, daily_losses, diebold_mariano, load, run, score

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="development", choices=sorted(PERIODS))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--context-days", default="7,28,90,160")
    parser.add_argument("--log", action="store_true", help="forecast log1p prices")
    parser.add_argument("--calendar", action="store_true", help="add calendar future covariates")
    parser.add_argument("--bids", action="store_true", help="add the lagged bid ratio as a past covariate")
    parser.add_argument("--baselines", action="store_true", help="include the naive baselines")
    parser.add_argument("--out", default="results/iex")
    args = parser.parse_args()

    models = dict(BASELINES) if args.baselines else {"naive_yesterday": BASELINES["naive_yesterday"]}
    for days in (int(d) for d in args.context_days.split(",")):
        common = {"context_days": days, "log": args.log}
        models[f"timesfm_{days}d"] = TimesFM(**common)
        if args.calendar:
            models[f"timesfm_{days}d_cal"] = TimesFM(**common, use_calendar=True)
        if args.bids:
            models[f"timesfm_{days}d_bid"] = TimesFM(**common, use_bids=True)
        if args.calendar and args.bids:
            models[f"timesfm_{days}d_cal_bid"] = TimesFM(**common, use_calendar=True, use_bids=True)

    started = time.perf_counter()
    results = run(load(), models, period=args.period, limit=args.limit)
    elapsed = time.perf_counter() - started

    board = score(results)
    days_scored = int(board["days"].iloc[0])
    _, device = session()
    print(f"{args.period}: {days_scored} days, {len(models)} models on {device}, {elapsed / 60:.1f} min\n")
    print(board[["days", "mae", "rmse", "bias", "mae_normal", "mae_at_cap", "rmae"]].round(1).to_string())
    losses = daily_losses(results)
    print("\nDiebold-Mariano against naive_yesterday:")
    for model in board.index:
        if model == "naive_yesterday":
            continue
        verdict = diebold_mariano(losses, model, "naive_yesterday")
        print(f"  {model:22} {'better' if verdict['better'] else 'worse':6}  p = {verdict['p_value']:.4f}")

    directory = Path(args.out)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{args.period}_timesfm.parquet"
    results.to_parquet(path, index=False)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
