"""Postgres store for issued forecasts and the scores they earn later.

A forecast is written once, at issue time, and never updated. Scores arrive in a
separate row after the delivery day settles, so nothing can quietly rewrite a
prediction once the answer is known.

Raw exchange prices are never stored here. The IEX terms are personal and
non-commercial, so what gets published is our own forecast and our own error.
"""

import json

from iex.backtest import BLOCKS
from iex.calibrate import COLUMNS


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
        " s.coverage_p10_p90 from iex_scores s join iex_forecasts f on f.id = s.forecast_id"
        " order by f.delivery_date desc limit %s", (days,)).fetchall()
    scored = [{"delivery_date": r[0], "model": r[1], "blocks_scored": r[2], "mae": r[3],
               "rmse": r[4], "bias": r[5], "coverage_p10_p90": r[6]} for r in rows]
    summary = None
    if scored:
        summary = {"days": len(scored),
                   "mae": sum(r["mae"] for r in scored) / len(scored),
                   "rmse": sum(r["rmse"] for r in scored) / len(scored)}
    return {"days": scored, "summary": summary}
