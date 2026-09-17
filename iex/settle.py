"""Score the forecasts we issued, once their delivery day has settled.

This never runs a model. It reads what was written at the cutoff, compares it to
what the market actually cleared, and writes the result to a separate table. A
day is only scored when the exchange has published all 96 blocks for it, so a
half-finished day cannot quietly flatter the record.

Scores are written once. If a day is already scored, it stays as it was.
"""

import argparse

import numpy as np
import pandas as pd

from iex import store
from iex.backtest import BLOCKS, load
from iex.calibrate import COLUMNS


def actuals_for(table, delivery_date):
    """The 96 cleared prices for a day, or None if the day has not settled."""
    day = table[(table["market"] == "dam")
                & (table["delivery_date"] == pd.Timestamp(delivery_date))]
    if len(day) != BLOCKS or not day["usable"].all():
        return None
    return day.sort_values("block")["price"].to_numpy()


def score_one(blocks, actual):
    """MAE, RMSE, bias and band coverage for one delivery day."""
    forecast = np.array([row["forecast"] for row in sorted(blocks, key=lambda r: r["block"])])
    error = forecast - actual
    scores = {"blocks_scored": len(actual), "mae": float(np.abs(error).mean()),
              "rmse": float(np.sqrt((error ** 2).mean())), "bias": float(error.mean()),
              "coverage_p10_p90": None}
    low, high = COLUMNS[0], COLUMNS[-1]
    if all(low in row and high in row for row in blocks):
        ordered = sorted(blocks, key=lambda r: r["block"])
        inside = [(row[low] <= value <= row[high]) for row, value in zip(ordered, actual, strict=True)]
        scores["coverage_p10_p90"] = float(np.mean(inside))
    return scores


def pending(connection):
    """Issued forecasts that have no score yet, oldest first."""
    rows = connection.execute(
        "select f.id, f.delivery_date from iex_forecasts f"
        " left join iex_scores s on s.forecast_id = f.id"
        " where s.forecast_id is null order by f.delivery_date").fetchall()
    return [(row[0], row[1]) for row in rows]


def settle(connection, table):
    """Score every settled day that is still waiting, and say what happened."""
    done, waiting = [], []
    for identifier, delivery_date in pending(connection):
        actual = actuals_for(table, delivery_date)
        if actual is None:
            waiting.append(delivery_date)
            continue
        record = store.for_day(connection, delivery_date)
        scores = score_one(record["blocks"], actual)
        store.record_score(connection, identifier, scores)
        done.append((delivery_date, scores))
    return done, waiting


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", type=int, default=10, help="how many scored days to print")
    args = parser.parse_args()

    from timesfm_serve import db

    table = load()
    db.open_pool()
    try:
        with db.conn() as connection:
            done, waiting = settle(connection, table)
            card = store.scorecard(connection, args.show)
    finally:
        db.pool.close()

    print(f"scored {len(done)} newly settled days, {len(waiting)} still waiting on the exchange")
    for delivery_date, scores in done:
        coverage = scores["coverage_p10_p90"]
        band = f", p10-p90 held {coverage:.0%}" if coverage is not None else ""
        print(f"  {delivery_date}  MAE {scores['mae']:7.1f}  RMSE {scores['rmse']:7.1f}"
              f"  bias {scores['bias']:+7.1f}{band}")
    if card["summary"]:
        print(f"\ntrack record over the last {card['summary']['days']} scored days: "
              f"MAE {card['summary']['mae']:.1f}, RMSE {card['summary']['rmse']:.1f}")


if __name__ == "__main__":
    main()
