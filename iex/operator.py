"""Grid-India's own day-ahead demand forecast error, as they publish it.

Article 31.2(i) of the Indian Electricity Grid Code 2023 makes the system operator
publish its all-India demand forecast error every day. That is the fairest outside
benchmark for a demand forecast on this grid: same series, same definition, and
produced by the people who also run the dispatch.

Their number is a day-ahead forecast. Ours is issued one day earlier still, because
the PSP report it reads only reaches D-2, so a tie is already a win on information.
"""

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

from iex.verify import GRID_API, GRID_CDN, get

STORE = Path("data/iex/operator")
FILE_TYPE = "DEMAND_FORECAST_ERROR_DAILY"
# Letters up to the end of 2025 report the error on the day's peak demand. From
# January 2026 they report it on demand met across the day, which is the same
# quantity we forecast, so only those are comparable.
ROW = re.compile(r"(Demand\s+Met|Peak\s+demand)\s+([\d.]+)\s*%\s+([\d.]+)\s*%", re.I)
FOR_DATE = re.compile(r"For\s+Date\s*:\s*(\d{2}-[A-Za-z]{3}-\d{4})")


def listing(year):
    payload = json.loads(get(GRID_API, {"_source": "GRDW", "_type": FILE_TYPE, "_fileDate": year}))
    items = payload if isinstance(payload, list) else payload.get("retData", [])
    days = {}
    for item in items:
        path, title = item.get("FilePath", ""), item.get("Title_", "")
        if not path.lower().endswith(".pdf"):
            continue
        try:
            day = pd.Timestamp(pd.to_datetime(title, format="%d-%b-%y"))
        except ValueError:
            continue
        days.setdefault(day, path)
    return days


def text(day, path):
    """Extracted text, cached; the PDFs themselves are 600 KB for two numbers."""
    local = STORE / "text" / f"{day:%Y%m%d}.txt"
    if local.exists():
        return local.read_text()
    import subprocess

    blob = get(GRID_CDN + path)
    done = subprocess.run(["pdftotext", "-layout", "-", "-"], input=blob, capture_output=True)
    if done.returncode:
        raise RuntimeError(f"{day:%Y-%m-%d}: pdftotext failed: {done.stderr.decode()[:200]}")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(done.stdout.decode("utf8", "ignore"))
    time.sleep(0.2)
    return local.read_text()


def parse(content, day):
    """The two percentages, checked against the date the letter states."""
    stated = FOR_DATE.search(content)
    if not stated:
        raise ValueError(f"{day:%Y-%m-%d}: no reporting date in the letter")
    reported = pd.Timestamp(pd.to_datetime(stated.group(1), format="%d-%b-%Y"))
    row = ROW.search(content)
    if not row:
        raise ValueError(f"{day:%Y-%m-%d}: no demand row")
    measure = "demand_met" if row.group(1).lower().startswith("demand") else "peak_demand"
    return {"delivery_date": reported, "listed_as": day, "measure": measure,
            "day_ahead_mape": float(row.group(2)), "real_time_mape": float(row.group(3))}


def build(first, last, workers=6):
    available = {}
    for year in _years(first, last):
        available |= listing(year)
    wanted = sorted(d for d in available if first <= d <= last + pd.Timedelta(days=2))

    def one(day):
        try:
            return parse(text(day, available[day]), day)
        except Exception as exc:
            return f"{day:%Y-%m-%d}: {type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(one, wanted))
    rows = [r for r in results if isinstance(r, dict)]
    skipped = [r for r in results if not isinstance(r, dict)]
    if not rows:
        raise RuntimeError(f"nothing parsed between {first:%Y-%m-%d} and {last:%Y-%m-%d}")

    table = pd.DataFrame(rows).drop_duplicates("delivery_date").sort_values("delivery_date")
    table = table[table["delivery_date"].between(first, last)].reset_index(drop=True)
    STORE.mkdir(parents=True, exist_ok=True)
    path = STORE / "demand_error.parquet"
    table.to_parquet(path, index=False)
    return table, skipped, path


def _years(first, last):
    start = first.year - (1 if first.month < 4 else 0)
    end = last.year - (1 if last.month < 4 else 0)
    return [f"{y}-{str(y + 1)[2:]}" for y in range(start, end + 1)]


def load():
    path = STORE / "demand_error.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run python -m iex.operator")
    return pd.read_parquet(path)


def our_demand_error(forecasts, actuals):
    """Our demand forecast scored the way Grid-India scores theirs.

    Their day-ahead figure for day D is issued on D-1, which is our lead of 2: the
    forecast made at the 09:30 cutoff on D-1 for the delivery day itself. Ours is
    the harder version of the same job, because the newest actuals it can see stop
    at D-2 while theirs run up to the moment they publish.
    """
    ours = forecasts[(forecasts["field"] == "demand") & (forecasts["lead"] == 2)]
    truth = actuals["demand"].rename("actual").reset_index()
    merged = ours.merge(truth, on=["delivery_date", "block"], how="inner")
    merged["ape"] = (merged["forecast"] - merged["actual"]).abs() / merged["actual"].abs().clip(lower=1)
    return (merged.groupby("delivery_date")["ape"].mean() * 100).rename("our_mape")


def compare(forecasts, actuals, published):
    """Both forecasts on the days where both exist."""
    ours = our_demand_error(forecasts, actuals)
    comparable = published[published["measure"] == "demand_met"]
    theirs = comparable.set_index("delivery_date")["day_ahead_mape"].rename("their_mape")
    both = pd.concat([ours, theirs], axis=1, sort=True).dropna()
    both["we_are_better_by"] = both["their_mape"] - both["our_mape"]
    return both


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="first", type=date.fromisoformat, default=date(2025, 2, 1))
    parser.add_argument("--to", dest="last", type=date.fromisoformat, default=date(2026, 9, 15))
    args = parser.parse_args()

    table, skipped, path = build(pd.Timestamp(args.first), pd.Timestamp(args.last))
    mismatched = table[table["delivery_date"] != table["listed_as"]]
    print(f"{len(table)} days, {table['delivery_date'].min():%Y-%m-%d} to "
          f"{table['delivery_date'].max():%Y-%m-%d}, {len(skipped)} skipped")
    print(f"{len(mismatched)} letters filed under a different day than they report on")
    for note in skipped[:5]:
        print(f"  {note}")
    print("\nGrid-India's own all-India demand forecast error, MAPE %")
    for measure, part in table.groupby("measure"):
        print(f"  {measure:11} {len(part):4} days, {part['delivery_date'].min():%Y-%m-%d} to "
              f"{part['delivery_date'].max():%Y-%m-%d}, day ahead {part['day_ahead_mape'].mean():5.2f}, "
              f"real time {part['real_time_mape'].mean():5.2f}")
    print("  they switched from peak demand to demand met in January 2026; only demand met is\n"
          "  the quantity we forecast, so only those days are compared below.")
    print(f"\nwrote {path}")

    from iex.driverforecast import load_forecasts
    from iex.drivers import load as load_drivers

    try:
        both = compare(load_forecasts(), load_drivers(), table)
    except FileNotFoundError as exc:
        print(f"\nno comparison yet: {exc}")
        return
    wins = int((both["we_are_better_by"] > 0).sum())
    print(f"\nAll-India demand forecast for the delivery day, MAPE %, over {len(both)} days both cover")
    print(f"  Grid-India, published daily under IEGC 31.2(i)   {both['their_mape'].mean():5.2f}")
    print(f"  ours, TimesFM zero-shot on their own actuals     {both['our_mape'].mean():5.2f}")
    print(f"  we are closer on {wins} of {len(both)} days ({wins / len(both):.0%})")
    print("  theirs is issued on D-1 with telemetry up to that moment; ours is issued at the\n"
          "  09:30 cutoff on D-1 and cannot see past D-2, so it forecasts a day further out.")


if __name__ == "__main__":
    main()
