import argparse
import time

import numpy as np
import pandas as pd
import timesfm

from timesfm_serve.data import WEATHER_COLS, load_state

QUANTILES = np.arange(0.1, 1.0, 0.1)


def mape(y, yhat):
    return np.mean(np.abs(y - yhat) / np.abs(y), axis=-1) * 100


def crps_from_quantiles(y, q):
    """pinball loss averaged over the 9 quantiles, a discrete crps proxy. y: (h,), q: (h, 9)"""
    diff = y[:, None] - q
    loss = np.maximum(QUANTILES * diff, (QUANTILES - 1) * diff)
    return 2 * loss.mean()


def run_state(model, state, origins, context, horizon):
    df = load_state(state)
    ctxs, covs, ys, naives = [], [], [], []
    for o in origins:
        i = df.index.get_loc(o)
        if i - context < 0 or i + horizon > len(df):
            continue
        hist, fut = df.iloc[i - context : i], df.iloc[i : i + horizon]
        ctxs.append(hist["demand"].to_numpy())
        covs.append(np.vstack([np.concatenate([hist[c].to_numpy(), fut[c].to_numpy()]) for c in WEATHER_COLS]))
        ys.append(fut["demand"].to_numpy())
        naives.append(np.tile(hist["demand"].to_numpy()[-7:], horizon // 7 + 1)[:horizon])

    y = np.array(ys)
    uni = list(model.predict_batch(ctxs, horizon=horizon, return_quantiles=True))
    wx = list(model.predict_batch(ctxs, horizon=horizon, past_future_covariates=covs, return_quantiles=True))

    rows = []
    for name, outs in [("timesfm", uni), ("timesfm+wx", wx)]:
        f = np.array([o.forecast for o in outs])
        q = np.array([o.quantiles for o in outs])
        rows.append(dict(state=state, model=name, mape=mape(y, f).mean(), crps=np.mean([crps_from_quantiles(y[k], q[k]) for k in range(len(y))]), n=len(y)))
    rows.append(dict(state=state, model="naive", mape=mape(y, np.array(naives)).mean(), crps=np.nan, n=len(y)))
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", nargs="+", default=["Karnataka", "Gujarat", "Delhi", "Maharashtra", "Tamil Nadu"])
    ap.add_argument("--start", default="2023-04-01")
    ap.add_argument("--end", default="2024-04-01")
    ap.add_argument("--step", type=int, default=7)
    ap.add_argument("--context", type=int, default=512)
    ap.add_argument("--horizon", type=int, default=14)
    ap.add_argument("--out", default=None, help="write per state results csv here")
    args = ap.parse_args()

    origins = pd.date_range(args.start, args.end, freq=f"{args.step}D")
    model = timesfm.TimesFM3Forecaster.from_pretrained("google/timesfm-3.0-pytorch", device="cpu")

    rows = []
    for s in args.states:
        t0 = time.time()
        rows += run_state(model, s, origins, args.context, args.horizon)
        print(f"{s}: {time.time() - t0:.1f}s", flush=True)

    res = pd.DataFrame(rows)
    if args.out:
        res.round(3).to_csv(args.out, index=False)
    print(f"\n{args.horizon} day ahead peak demand, weekly origins {args.start} to {args.end}, context {args.context} days")
    print(res.pivot(index="state", columns="model", values="mape").round(2).to_string())
    print("\ncrps (lower is better)")
    print(res.pivot(index="state", columns="model", values="crps").dropna(axis=1).round(0).to_string())
    print("\nmean mape:", res.groupby("model")["mape"].mean().round(2).to_dict())
