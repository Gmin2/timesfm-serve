"""Postgres store for issued forecasts and the scores they earn later.

A forecast is written once, at issue time, and never updated. Scores arrive in a
separate row after the delivery day settles, so nothing can quietly rewrite a
prediction once the answer is known.

Raw exchange prices are never stored here. The IEX terms are personal and
non-commercial, so what gets published is our own forecast and our own error.
"""

import json
from datetime import timedelta

from iex.backtest import BLOCKS
from iex.calibrate import COLUMNS

# A forecast written within a few hours of its own cutoff was issued live. One
# written long afterwards is a backfill: still leakage-free, because it is built
# through the same History object, but it was not a standing prediction and must
# never be counted as one.
LIVE_WINDOW = timedelta(hours=6)


def record(connection, delivery_date, model, cutoff_at, forecast, quantiles=None, revision=""):
    """Store one day's 96 blocks. Re-issuing the same day is refused, not merged."""
    if len(forecast) != BLOCKS:
        raise ValueError(f"expected {BLOCKS} blocks, got {len(forecast)}")
    blocks = []
    for index in range(BLOCKS):
        row = {"block": index + 1, "forecast": round(float(forecast[index]), 2)}
        if quantiles is not None:
            row |= {name: round(float(quantiles[index][position]), 2)
                    for position, name in enumerate(COLUMNS)}
        blocks.append(row)
    found = connection.execute(
        "insert into iex_forecasts (delivery_date, model, cutoff_at, blocks, revision)"
        " values (%s, %s, %s, %s, %s)"
        " on conflict (delivery_date, model) do nothing returning id",
        (delivery_date, model, cutoff_at, json.dumps(blocks), revision)).fetchone()
    return found[0] if found else None


def latest(connection, model=None):
    """The most recent delivery day we have issued a forecast for."""
    if model:
        found = connection.execute(
            "select id, delivery_date, model, issued_at, cutoff_at, blocks, revision"
            " from iex_forecasts where model = %s order by delivery_date desc limit 1",
            (model,)).fetchone()
    else:
        found = connection.execute(
            "select id, delivery_date, model, issued_at, cutoff_at, blocks, revision"
            " from iex_forecasts order by delivery_date desc, id desc limit 1").fetchone()
    return _shape(found)


def for_day(connection, delivery_date, model=None):
    sql = ("select id, delivery_date, model, issued_at, cutoff_at, blocks, revision"
           " from iex_forecasts where delivery_date = %s")
    args = [delivery_date]
    if model:
        sql += " and model = %s"
        args.append(model)
    return _shape(connection.execute(sql + " order by id desc limit 1", args).fetchone())


def _shape(row):
    if row is None:
        return None
    identifier, delivery_date, model, issued_at, cutoff_at, blocks, revision = row
    return {"id": identifier, "delivery_date": delivery_date, "model": model,
            "issued_at": issued_at, "cutoff_at": cutoff_at,
            "issued_live": issued_at <= cutoff_at + LIVE_WINDOW,
            "blocks": blocks if isinstance(blocks, list) else json.loads(blocks),
            "revision": revision}


def record_score(connection, forecast_id, scores):
    connection.execute(
        "insert into iex_scores (forecast_id, blocks_scored, mae, rmse, bias, coverage_p10_p90)"
        " values (%s, %s, %s, %s, %s, %s) on conflict (forecast_id) do nothing",
        (forecast_id, scores["blocks_scored"], scores["mae"], scores["rmse"],
         scores["bias"], scores.get("coverage_p10_p90")))


def scorecard(connection, days=30):
    """Every scored day, newest first, plus the running average."""
    rows = connection.execute(
        "select f.delivery_date, f.model, s.blocks_scored, s.mae, s.rmse, s.bias,"
        " s.coverage_p10_p90, f.issued_at, f.cutoff_at"
        " from iex_scores s join iex_forecasts f on f.id = s.forecast_id"
        " order by f.delivery_date desc limit %s", (days,)).fetchall()
    scored = [{"delivery_date": r[0], "model": r[1], "blocks_scored": r[2], "mae": r[3],
               "rmse": r[4], "bias": r[5], "coverage_p10_p90": r[6],
               "issued_live": r[7] <= r[8] + LIVE_WINDOW} for r in rows]
    return {"days": scored, "summary": _summary(scored),
            "live_only": _summary([r for r in scored if r["issued_live"]])}


def _summary(scored):
    """Averages, kept separate for live days so a backfill cannot pad the record."""
    if not scored:
        return None
    return {"days": len(scored),
            "mae": sum(r["mae"] for r in scored) / len(scored),
            "rmse": sum(r["rmse"] for r in scored) / len(scored),
            "coverage_p10_p90": (
                sum(r["coverage_p10_p90"] for r in scored if r["coverage_p10_p90"] is not None)
                / max(sum(1 for r in scored if r["coverage_p10_p90"] is not None), 1))}


def settled_results(connection, before, model):
    """Past forecasts next to what actually cleared, for calibrating the next one.

    Only days strictly before the one being forecast, and only days that have
    already been scored, so the correction is fitted on settled history alone.
    """
    import pandas as pd

    rows = connection.execute(
        "select f.delivery_date, f.blocks from iex_forecasts f"
        " join iex_scores s on s.forecast_id = f.id"
        " where f.model = %s and f.delivery_date < %s"
        " order by f.delivery_date desc limit 180", (model, before)).fetchall()
    if not rows:
        return None
    frames = []
    for delivery_date, blocks in rows:
        blocks = blocks if isinstance(blocks, list) else json.loads(blocks)
        frame = pd.DataFrame(sorted(blocks, key=lambda r: r["block"]))
        frame["delivery_date"] = pd.Timestamp(delivery_date)
        frame["model"] = model
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def models(connection):
    """Every model with forecasts on record, newest activity first."""
    rows = connection.execute(
        "select model, count(*), min(delivery_date), max(delivery_date), max(revision)"
        " from iex_forecasts group by model order by max(delivery_date) desc").fetchall()
    return [{"name": r[0], "delivery_days": r[1], "first_delivery_date": r[2],
             "last_delivery_date": r[3], "revision": r[4]} for r in rows]
