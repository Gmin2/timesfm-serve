"""Conformal calibration of TimesFM's quantiles, using only days already settled.

The published quantiles are systematically narrow on this market, so a p10-p90
band that should hold 80% of prices holds less. For each level, the correction is
the empirical quantile of past residuals: how far the actual price tended to fall
from that quantile on days whose outcome was already known at the cutoff.
"""

import numpy as np
import pandas as pd

from iex.backtest import BLOCKS, QUANTILE_LEVELS

COLUMNS = [f"q{int(level * 100)}" for level in QUANTILE_LEVELS]
MIN_CALIBRATION_DAYS = 30


def coverage(results):
    """Share of prices at or below each quantile, against the level it claims."""
    rows = []
    for level, column in zip(QUANTILE_LEVELS, COLUMNS, strict=True):
        observed = (results["actual"] <= results[column]).mean()
        rows.append({"level": level, "target": level, "observed": float(observed),
                     "gap": float(observed - level)})
    frame = pd.DataFrame(rows)
    inside = ((results["actual"] >= results[COLUMNS[0]]) & (results["actual"] <= results[COLUMNS[-1]])).mean()
    return frame, float(inside)


def offsets(past):
    """One additive correction per level, from residuals on settled days only."""
    corrections = {}
    for level, column in zip(QUANTILE_LEVELS, COLUMNS, strict=True):
        residuals = (past["actual"] - past[column]).to_numpy()
        corrections[column] = float(np.quantile(residuals, level))
    return corrections


def calibrate(results, window=90, cap=10_000.0):
    """Rolling conformal adjustment, one delivery day at a time.

    The correction for a day is fitted only on days strictly before it, and needs
    at least MIN_CALIBRATION_DAYS of them, so early days pass through unchanged
    and are marked so they can be excluded from a fair comparison.
    """
    if not set(COLUMNS) <= set(results.columns):
        raise ValueError("results carry no quantiles; run a model that exposes them")
    days = sorted(results["delivery_date"].unique())
    pieces = []
    for day in days:
        current = results[results["delivery_date"] == day].copy()
        past = results[results["delivery_date"] < day]
        settled = sorted(past["delivery_date"].unique())[-window:]
        past = past[past["delivery_date"].isin(settled)]
        if len(settled) < MIN_CALIBRATION_DAYS:
            current["calibrated"] = False
            pieces.append(current)
            continue
        for column, correction in offsets(past).items():
            current[column] = np.clip(current[column] + correction, 0, cap)
        # Corrections are fitted per level, so the result can cross; sorting keeps
        # the quantiles monotone without changing any individual level's coverage much.
        values = np.sort(current[COLUMNS].to_numpy(), axis=1)
        current[COLUMNS] = values
        current["forecast"] = values[:, len(COLUMNS) // 2]
        current["calibrated"] = True
        pieces.append(current)
    return pd.concat(pieces, ignore_index=True)


def pinball(results):
    """Mean pinball loss across the nine levels, the usual score for quantiles."""
    total = 0.0
    for level, column in zip(QUANTILE_LEVELS, COLUMNS, strict=True):
        error = results["actual"] - results[column]
        total += np.maximum(level * error, (level - 1) * error).mean()
    return float(total / len(QUANTILE_LEVELS))


def cap_probability(results, cap=10_000.0):
    """Chance a block clears the cap, read off the quantiles, scored with Brier."""
    quantiles = results[COLUMNS].to_numpy()
    above = (quantiles >= cap - 0.01)
    # The lowest level whose quantile reaches the cap bounds the probability.
    probability = np.where(above.any(axis=1), 1.0 - np.array(QUANTILE_LEVELS)[above.argmax(axis=1)], 0.0)
    outcome = results["at_cap"].to_numpy().astype(float)
    return {"brier": float(np.mean((probability - outcome) ** 2)),
            "base_rate": float(outcome.mean()),
            "climatology_brier": float(np.mean((outcome.mean() - outcome) ** 2))}


def main():
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results/iex/development_timesfm.parquet")
    parser.add_argument("--model", default="timesfm_90d_cal_wx")
    parser.add_argument("--window", type=int, default=90)
    args = parser.parse_args()

    everything = pd.read_parquet(args.results)
    results = everything[everything["model"] == args.model].copy()
    if results.empty:
        raise SystemExit(f"no rows for {args.model}; have {sorted(everything['model'].unique())}")

    adjusted = calibrate(results, window=args.window)
    scored = adjusted[adjusted["calibrated"]]
    raw = results[results["delivery_date"].isin(scored["delivery_date"].unique())]

    before, inside_before = coverage(raw)
    after, inside_after = coverage(scored)
    print(f"{args.model}, {scored['delivery_date'].nunique()} days with a {args.window}-day "
          f"calibration window\n")
    print(f"{'level':>7}{'target':>9}{'before':>9}{'after':>9}")
    for (_, first), (_, second) in zip(before.iterrows(), after.iterrows(), strict=True):
        print(f"{first['level']:7.1f}{first['target']:9.0%}{first['observed']:9.1%}{second['observed']:9.1%}")
    print(f"\np10-p90 band, should hold 80%: {inside_before:.1%} before, {inside_after:.1%} after")
    print(f"mean pinball loss:            {pinball(raw):8.1f} before, {pinball(scored):8.1f} after")
    print(f"median MAE:                   {(raw['forecast'] - raw['actual']).abs().mean():8.1f} before, "
          f"{(scored['forecast'] - scored['actual']).abs().mean():8.1f} after")

    cap = cap_probability(scored)
    print(f"\nprobability a block hits the cap: Brier {cap['brier']:.4f} "
          f"against {cap['climatology_brier']:.4f} for always guessing the base rate "
          f"of {cap['base_rate']:.1%}")

    path = Path(args.results).with_name(Path(args.results).stem + "_calibrated.parquet")
    adjusted.to_parquet(path, index=False)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
