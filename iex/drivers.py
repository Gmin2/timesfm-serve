"""All-India grid drivers from Grid-India's daily PSP report.

The PSP report carries a TimeSeries sheet at exactly the 96 fifteen-minute blocks
the exchange settles on: demand met, and the generation that served it split by
fuel. Net demand, demand minus wind minus solar, is what the dispatchable fleet
has to cover, and it is the quantity the day-ahead price is really about.

These are actuals, not forecasts. The report for day X is published early on X+1,
so at the 09:30 cutoff on D-1 the newest one available covers D-2. DriverHistory
enforces that; anything the delivery day itself knows is off limits unless it is
asked for explicitly as a ceiling.
"""

import argparse
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

from iex.backtest import BLOCKS, LeakageError
from iex.verify import GRID_API, GRID_CDN, get

STORE = Path("data/iex/psp")
FILE_TYPE = "DAILY_PSP_REPORT"
# Header keywords in the TimeSeries sheet, in the order they appear. The sheet is
# matched on these rather than on position, because the column count has changed
# before and would silently shift everything by one.
FIELDS = {
    "frequency": "frequency",
    "demand": "demand met",
    "storage_demand": "storage demand",
    "nuclear": "nuclear",
    "wind": "wind",
    "solar": "solar",
    "hydro": "hydro",
    "gas": "gas",
    "thermal": "thermal",
    "storage_gen": "storage (psp",
    "others": "others",
    "net_demand": "net demand met",
    "generation": "total generation",
}
# Generation columns that total generation is the sum of. Grid-India widened the
# sheet on 2026-05-26 to split storage out of "others", so the two storage columns
# are only present on later days and the sum has to follow.
GENERATION = ("nuclear", "wind", "solar", "hydro", "gas", "thermal", "storage_gen", "others")
# A day is dropped if the published numbers disagree with each other by more than
# this. National demand runs near 200 GW, so it is a fifth of a percent.
TOLERANCE = 500.0
# Reports before this date have no TimeSeries sheet at all.
FIRST_DAY = pd.Timestamp("2024-11-04")


def listing(year):
    """Every daily PSP spreadsheet for a financial year, newest first."""
    payload = json.loads(get(GRID_API, {"_source": "GRDW", "_type": FILE_TYPE, "_fileDate": year}))
    items = payload if isinstance(payload, list) else payload.get("retData", [])
    days = {}
    for item in items:
        path = item.get("FilePath", "")
        stamp = re.search(r"(\d{2})\.(\d{2})\.(\d{2})", item.get("Title_", ""))
        if not stamp or not path.lower().endswith((".xls", ".xlsx")):
            continue
        day = pd.Timestamp(2000 + int(stamp.group(3)), int(stamp.group(2)), int(stamp.group(1)))
        # Some days are listed twice, once under a legacy path that 404s.
        if day not in days or re.search(r"/\d{4}/\d{2}/", path):
            days[day] = path
    return days


def years(first, last):
    """Financial years, April to March, covering a date range."""
    start = first.year - (1 if first.month < 4 else 0)
    end = last.year - (1 if last.month < 4 else 0)
    return [f"{y}-{str(y + 1)[2:]}" for y in range(start, end + 1)]


def fetch(day, path):
    """One day's spreadsheet, cached on disk exactly as served."""
    local = STORE / "raw" / f"{day:%Y%m%d}{Path(path).suffix.lower()}"
    if local.exists():
        return local.read_bytes()
    blob = get(GRID_CDN + path)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(blob)
    time.sleep(0.2)
    return blob


def parse(blob, day):
    """The TimeSeries sheet as 96 rows, one per block."""
    book = pd.ExcelFile(io.BytesIO(blob) if isinstance(blob, bytes) else blob)
    if "TimeSeries" not in book.sheet_names:
        raise KeyError(f"{day:%Y-%m-%d} has no TimeSeries sheet")
    raw = book.parse("TimeSeries", header=None)
    flat = raw.map(lambda v: re.sub(r"\s+", " ", str(v)).strip().lower())
    header = next((i for i, row in flat.iterrows()
                   if row.str.startswith("time").any() and row.str.contains("demand").any()), None)
    if header is None:
        raise KeyError(f"{day:%Y-%m-%d} TimeSeries has no header row")

    names = flat.loc[header]
    columns = {}
    for field, keyword in FIELDS.items():
        # "net demand met" also matches "demand met", so take the longest match.
        hits = [c for c in names.index if keyword in names[c]]
        if field == "demand":
            hits = [c for c in hits if "net" not in names[c] and "storage" not in names[c]]
        if hits:
            columns[field] = max(hits, key=lambda c: len(names[c]))

    missing = {"demand", "wind", "solar"} - set(columns)
    if missing:
        raise KeyError(f"{day:%Y-%m-%d} TimeSeries is missing {sorted(missing)}")

    time_column = next(c for c in names.index if names[c].startswith("time"))
    body = raw.loc[header + 1:]
    clock = pd.to_datetime(body[time_column].astype(str).str.strip(), format="mixed", errors="coerce")
    body = body[clock.notna()]
    if len(body) != BLOCKS:
        raise ValueError(f"{day:%Y-%m-%d} TimeSeries has {len(body)} rows, expected {BLOCKS}")

    frame = pd.DataFrame({field: pd.to_numeric(body[c], errors="coerce").to_numpy()
                          for field, c in columns.items()})
    frame.insert(0, "delivery_date", day)
    frame.insert(1, "block", range(1, BLOCKS + 1))
    return frame


def build(first, last, workers=6):
    """Download and parse every day in a range, skipping days with no sheet."""
    available = {}
    for year in years(first, last):
        available |= listing(year)
    wanted = sorted(d for d in available if first <= d <= last)

    def one(day):
        try:
            return parse(fetch(day, available[day]), day)
        except Exception as exc:
            return f"{day:%Y-%m-%d}: {type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(one, wanted))
    frames = [r for r in results if isinstance(r, pd.DataFrame)]
    skipped = [r for r in results if not isinstance(r, pd.DataFrame)]
    if not frames:
        raise RuntimeError(f"nothing parsed between {first:%Y-%m-%d} and {last:%Y-%m-%d}")

    table = pd.concat(frames, ignore_index=True).sort_values(["delivery_date", "block"])
    table = table.set_index(["delivery_date", "block"])
    STORE.mkdir(parents=True, exist_ok=True)
    path = STORE / "drivers.parquet"
    table.to_parquet(path)
    return table, skipped, path


def usable_days(table):
    """Day -> whether the published numbers for that day can be trusted."""
    return day_flags(table)["usable"].to_dict()


def load():
    path = STORE / "drivers.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run python -m iex.drivers --from ... --to ...")
    return pd.read_parquet(path)


def day_flags(table):
    """Per day: the worst internal disagreement, and whether to trust the day.

    Grid-India occasionally publishes a day with a broken block or a column that
    does not add up. Rather than quietly forecasting through it, each day is
    checked against the sheet's own two identities and marked.
    """
    wide = table.reset_index()
    gaps = pd.DataFrame({"delivery_date": wide["delivery_date"]})
    if {"demand", "wind", "solar", "net_demand"} <= set(wide.columns):
        gaps["net_gap"] = (wide["net_demand"] - (wide["demand"] - wide["wind"] - wide["solar"])).abs()
    present = [f for f in GENERATION if f in wide.columns]
    if "generation" in wide.columns and present:
        gaps["generation_gap"] = (wide["generation"] - wide[present].sum(axis=1)).abs()
    # Physically impossible readings mean a broken row whatever the totals say.
    gaps["impossible"] = (wide["demand"] <= 0) | (wide.get("solar", 0) < -100)
    if "thermal" in wide.columns:
        gaps["impossible"] |= wide["thermal"] <= 0

    worst = gaps.groupby("delivery_date").max()
    # Usability turns on the identity the price model actually depends on, net
    # demand against demand minus wind minus solar, plus the physical sanity of
    # the row. The generation identity is reported but not enforced: Grid-India
    # began counting storage in the total three weeks before publishing the
    # column for it, which breaks the sum on days whose demand is perfectly good.
    worst["usable"] = ~worst["impossible"]
    if "net_gap" in worst.columns:
        worst["usable"] &= worst["net_gap"] <= TOLERANCE
    return worst


def check(table):
    """Internal cross-checks on the published numbers, worst case in MW."""
    flags = day_flags(table)
    rows = []
    if "net_gap" in flags:
        rows.append(("net demand equals demand minus wind minus solar", float(flags["net_gap"].max())))
    if "generation_gap" in flags:
        rows.append(("total generation equals the fuel columns summed", float(flags["generation_gap"].max())))
    return rows


class DriverHistory:
    """Read-only view of the drivers, stopping two days before delivery.

    Prices for D-1 are known at the cutoff but the PSP report for D-1 is not, so
    this reaches one day less far back than the price History does.
    """

    def __init__(self, table, delivery_date, usable=None):
        self.delivery_date = pd.Timestamp(delivery_date)
        self.cutoff = self.delivery_date - pd.Timedelta(days=2)
        self._table = table
        self._usable = usable

    def series(self, field, days=None):
        """One driver as a day-by-block frame, oldest first, on a gap-free grid.

        Days Grid-India never published, or published broken, are filled by
        interpolating between the good days on either side. Every day used for
        that is itself at or before the cutoff, so the fill cannot see forward.
        """
        wide = self._table[field].unstack("block").reindex(columns=range(1, BLOCKS + 1))
        wide = wide[wide.index <= self.cutoff]
        if len(wide) and wide.index.max() > self.cutoff:
            raise LeakageError(f"drivers reach {wide.index.max()}, past cutoff {self.cutoff}")
        if self._usable is not None:
            wide = wide[wide.index.map(lambda d: bool(self._usable.get(d, False)))]
        if not len(wide):
            return wide
        # Run to the cutoff even when the last report or two never appeared, so the
        # series always ends where the forecast starts. Without this a missing D-2
        # shortens the series and the horizon lands on the wrong days.
        whole = wide.reindex(pd.date_range(wide.index.min(), self.cutoff, freq="D"))
        whole = whole.interpolate(axis=0, limit_direction="both").ffill().bfill()
        whole.index.name = "delivery_date"
        return whole.tail(days) if days else whole

    def filled_days(self):
        """Days inside the usable span that had to be interpolated."""
        published = {d for d in self._table.index.get_level_values("delivery_date").unique()
                     if d <= self.cutoff and (self._usable is None or self._usable.get(d, False))}
        if not published:
            return []
        span = pd.date_range(min(published), max(published), freq="D")
        return [d for d in span if d not in published]

    def horizon(self):
        """Days from the last known driver day to delivery, always 2."""
        return (self.delivery_date - self.cutoff).days


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="first", type=date.fromisoformat, default=FIRST_DAY.date())
    parser.add_argument("--to", dest="last", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()

    table, skipped, path = build(pd.Timestamp(args.first), pd.Timestamp(args.last))
    days = table.index.get_level_values("delivery_date")
    span = pd.date_range(days.min(), days.max(), freq="D")
    print(f"{len(table):,} blocks over {days.nunique()} days, {days.min():%Y-%m-%d} to {days.max():%Y-%m-%d}")
    print(f"{len(span) - days.nunique()} days missing inside that span, {len(skipped)} skipped")
    for note in skipped[:5]:
        print(f"  {note}")

    flags = day_flags(table)
    print("\ncross-checks, worst disagreement in MW across all days")
    for label, worst in check(table):
        print(f"  {label:<52} {worst:9.1f}")
    bad = flags[~flags["usable"]]
    print(f"\n{int(flags['usable'].sum())} days usable, {len(bad)} dropped")
    for day, row in bad.iterrows():
        reasons = []
        if "net_gap" in row and row["net_gap"] > TOLERANCE:
            reasons.append(f"net demand off by {row['net_gap']:.0f} MW")
        if row.get("impossible"):
            reasons.append("a block reads as physically impossible")
        print(f"  {day:%Y-%m-%d}  {', '.join(reasons)}")
    noted = flags[flags["usable"] & (flags.get("generation_gap", 0) > TOLERANCE)]
    if len(noted):
        print(f"\n{len(noted)} usable days where the fuel columns do not sum to total generation. "
              f"Nearly all are in May 2026, when storage was already counted in the total but had "
              f"no column of its own yet. Demand, wind and solar are self-consistent on all of them.")

    good = table[table.index.get_level_values("delivery_date").isin(flags[flags["usable"]].index)]
    print(f"\n{good.describe().round(0).to_string()}")
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
