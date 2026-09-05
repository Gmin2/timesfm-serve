"""how much is a weather forecast worth to a demand forecast?

five methods, same origins, same targets:

  naive              repeat the same weekday from last week
  gbt + temp forecast  gradient boosted trees on demand lags, temperature and calendar
  timesfm            the foundation model, demand history only
  timesfm + temp forecast  ... plus the temperature forecast that existed on the day
  timesfm + temp actual    ... plus the temperature that actually happened
  timesfm + all actual     ... plus temperature, irradiance and wind, all perfect

the middle three hold the variable set fixed at temperature and vary only
foresight, so the gap between them is exactly what forecast error costs. only
temperature can be compared this way: open-meteo's previous runs archive
carries no radiation or wind for these locations, so there is no lead-time
correct version of them to compare against.

the last row is the number an evaluation built on reanalysis actuals would
report. it is a ceiling nobody can reach, and the distance from it to the
forecast row is the honest measure of what a better weather model is worth.
"""

import argparse
import time

import numpy as np
import pandas as pd
import timesfm
from sklearn.ensemble import HistGradientBoostingRegressor

from scripts import covariates as cv
from timesfm_serve.data import load_state

QUANTILES = np.arange(0.1, 1.0, 0.1)
ALL_FEATURES = cv.FEATURES
TEMP = cv.TEMP_FEATURES


def mape(y, yhat):
    return float(np.mean(np.abs(y - yhat) / np.abs(y)) * 100)


def crps(y, q):
    """pinball loss averaged over the nine quantiles, a discrete crps"""
    diff = y[:, None] - q
    return float(2 * np.maximum(QUANTILES * diff, (QUANTILES - 1) * diff).mean())


def coverage(y, q):
    """fraction of actuals inside the p10 to p90 band. should be 0.8."""
    return float(np.mean((y >= q[:, 0]) & (y <= q[:, 8])))


def calendar(index: pd.DatetimeIndex) -> pd.DataFrame:
    doy = index.dayofyear
    return pd.DataFrame(
        {
            "dow": index.dayofweek,
            "month": index.month,
            "doy_sin": np.sin(2 * np.pi * doy / 365.25),
            "doy_cos": np.cos(2 * np.pi * doy / 365.25),
        },
        index=index,
    )


def gbt_forecast(demand, hist_idx, fut_idx, hist_wx, future_wx):
    """a competent engineer's afternoon: trees on demand lags, temperature and
    calendar, refit on the context window at every origin.

    the lag features matter. trees cannot extrapolate a trend, so without a
    recent demand level to anchor on, a tree model predicts the mean of the
    training window and loses to repeating last week.
    """

    def features(index, wx):
        lag7 = demand.reindex(index - pd.Timedelta(days=7)).to_numpy()
        lag14 = demand.reindex(index - pd.Timedelta(days=14)).to_numpy()
        roll = demand.reindex(index - pd.Timedelta(days=7)).rolling(28, min_periods=1).mean().to_numpy()
        return np.column_stack(
            [wx[TEMP].to_numpy(), calendar(index).to_numpy(), lag7, lag14, roll]
        )

    model = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.06, random_state=0)
    model.fit(features(hist_idx, hist_wx), demand.loc[hist_idx].to_numpy())
    return model.predict(features(fut_idx, future_wx))


def stack(hist_wx, future_wx, features):
    """[feature][context + horizon]. the history is always what the weather
    actually was, because on the day you make the forecast you know the past.
    only the future half changes between the runs."""
    return np.vstack(
        [
            np.concatenate([hist_wx[f].to_numpy(), future_wx[f].to_numpy()])
            for f in features
        ]
    ).astype(np.float32)


def run_state(model, state, origins, context, horizon):
    demand = load_state(state)["demand"]
    span_start = (origins[0] - pd.Timedelta(days=context + 5)).date()
    span_end = (origins[-1] + pd.Timedelta(days=horizon + 5)).date()

    actual_wx = cv.actuals(state, span_start, span_end)
    leads = cv.forecast_by_lead(state, span_start, span_end)
    bias = cv.rolling_bias(actual_wx, leads)

    ctxs, ys, naives, gbts = [], [], [], []
    cov_temp_forecast, cov_temp_actual, cov_all_actual = [], [], []

    for origin in origins:
        hist_idx = pd.date_range(end=origin, periods=context, freq="D")
        fut_idx = pd.date_range(start=origin + pd.Timedelta(days=1), periods=horizon, freq="D")
        if not (hist_idx.isin(demand.index).all() and fut_idx.isin(demand.index).all()):
            continue
        if not hist_idx.isin(actual_wx.index).all():
            continue
        if not fut_idx.isin(actual_wx.index).all():
            continue
        # thin days are NaN now rather than interpolated, so skip any origin
        # whose window is not fully covered by real observations
        if actual_wx.loc[hist_idx].isna().any().any():
            continue
        if actual_wx.loc[fut_idx].isna().any().any():
            continue

        future_forecast = cv.horizon_covariates_corrected(leads, bias, origin, horizon)
        if future_forecast.empty:
            continue

        hist_demand = demand.loc[hist_idx]
        hist_wx = actual_wx.loc[hist_idx]
        future_actual = actual_wx.loc[fut_idx]

        ctxs.append(hist_demand.to_numpy(dtype=np.float32))
        ys.append(demand.loc[fut_idx].to_numpy())
        naives.append(np.tile(hist_demand.to_numpy()[-7:], horizon // 7 + 1)[:horizon])
        gbts.append(gbt_forecast(demand, hist_idx, fut_idx, hist_wx, future_forecast))

        cov_temp_forecast.append(stack(hist_wx, future_forecast, TEMP))
        cov_temp_actual.append(stack(hist_wx, future_actual, TEMP))
        cov_all_actual.append(stack(hist_wx, future_actual, ALL_FEATURES))

    if not ctxs:
        return []

    y = np.array(ys)
    def batch(cov=None):
        kw = {"past_future_covariates": cov} if cov is not None else {}
        return list(model.predict_batch(ctxs, horizon=horizon, return_quantiles=True, **kw))

    runs = {
        "timesfm": batch(),
        "timesfm+temp forecast": batch(cov_temp_forecast),
        "timesfm+temp actual": batch(cov_temp_actual),
        "timesfm+all actual": batch(cov_all_actual),
    }

    rows = [
        dict(state=state, model="naive", mape=np.mean([mape(a, b) for a, b in zip(y, naives, strict=True)]), crps=np.nan, coverage=np.nan, n=len(y)),
        dict(state=state, model="gbt+temp forecast", mape=np.mean([mape(a, b) for a, b in zip(y, gbts, strict=True)]), crps=np.nan, coverage=np.nan, n=len(y)),
    ]
    for name, outs in runs.items():
        point = np.array([o.forecast for o in outs])
        quant = np.array([o.quantiles for o in outs])
        rows.append(
            dict(
                state=state,
                model=name,
                mape=np.mean([mape(y[i], point[i]) for i in range(len(y))]),
                crps=np.mean([crps(y[i], quant[i]) for i in range(len(y))]),
                coverage=np.mean([coverage(y[i], quant[i]) for i in range(len(y))]),
                n=len(y),
            )
        )
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", nargs="+", default=list(cv.COORDS))
    ap.add_argument("--start", default="2023-04-01")
    ap.add_argument("--end", default="2024-04-01")
    ap.add_argument("--step", type=int, default=7)
    ap.add_argument("--context", type=int, default=512)
    ap.add_argument("--horizon", type=int, default=7)
    ap.add_argument("--device", default="cpu", help="mps on an apple laptop is about 2x")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    origins = pd.date_range(args.start, args.end, freq=f"{args.step}D")
    model = timesfm.TimesFM3Forecaster.from_pretrained(
        "google/timesfm-3.0-pytorch", device=args.device
    )

    rows = []
    for s in args.states:
        t0 = time.time()
        rows += run_state(model, s, origins, args.context, args.horizon)
        print(f"{s}: {time.time() - t0:.1f}s", flush=True)

    res = pd.DataFrame(rows)
    if args.out:
        res.round(3).to_csv(args.out, index=False)

    order = ["naive", "gbt+temp forecast", "timesfm", "timesfm+temp forecast",
             "timesfm+temp actual", "timesfm+all actual"]
    print(f"\n{args.horizon} day ahead peak demand, origins {args.start} to {args.end}, "
          f"context {args.context} days, n={int(res['n'].iloc[0])} per state")
    for metric, label in (("mape", "mape %"), ("crps", "crps"), ("coverage", "p10-p90 coverage")):
        table = res.pivot(index="state", columns="model", values=metric)
        table = table[[c for c in order if c in table.columns]].dropna(axis=1, how="all")
        print(f"\n{label}")
        print(table.round(3).to_string())
    print("\nmean mape:", res.groupby("model")["mape"].mean().round(2).reindex(order).to_dict())
