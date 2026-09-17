import io

import numpy as np
import pandas as pd
import pytest
import xlwt

from iex.backtest import BLOCKS
from iex.drivers import DriverHistory, check, listing, parse, years


def sheet(day, rows=BLOCKS, header_offset=3, extra_column=False):
    """A spreadsheet shaped like Grid-India's, written the way they write it."""
    book = xlwt.Workbook()
    page = book.add_sheet("TimeSeries")
    page.write(0, 0, "Date of Reporting:")
    page.write(0, 14, day.strftime("%d-%b-%Y"))
    names = ["TIME", "FREQUENCY\n(Hz)", "DEMAND\nMET¹\n(MW)", "STORAGE\nDEMAND\n(MW)",
             "NUCLEAR\n(MW)", "WIND\n(MW)", "SOLAR\n(MW)", "HYDRO²\n(MW)", "GAS\n(MW)",
             "THERMAL\n(MW)", "STORAGE\n(PSP&BESS)", "OTHERS³\n(MW)",
             "NET DEMAND MET\n(MW)", "TOTAL GENERATION\n(MW)"]
    if extra_column:
        names.insert(1, "SOMETHING NEW")
    for column, name in enumerate(names):
        page.write(header_offset, column, name)
    for row in range(rows):
        values = {"TIME": f"{row // 4}:{15 * (row % 4):02d}", "FREQUENCY\n(Hz)": 50.0,
                  "DEMAND\nMET¹\n(MW)": 200_000 + row, "STORAGE\nDEMAND\n(MW)": 10,
                  "NUCLEAR\n(MW)": 7000, "WIND\n(MW)": 5000, "SOLAR\n(MW)": 100 * row,
                  "HYDRO²\n(MW)": 20_000, "GAS\n(MW)": 3000, "THERMAL\n(MW)": 150_000,
                  "STORAGE\n(PSP&BESS)": 500, "OTHERS³\n(MW)": 1000,
                  "SOMETHING NEW": 0}
        values["NET DEMAND MET\n(MW)"] = values["DEMAND\nMET¹\n(MW)"] - 5000 - 100 * row
        values["TOTAL GENERATION\n(MW)"] = (7000 + 5000 + 100 * row + 20_000 + 3000
                                            + 150_000 + 500 + 1000)
        for column, name in enumerate(names):
            page.write(header_offset + 1 + row, column, values[name])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def table(first="2025-01-01", days=40):
    rows = []
    for offset, day in enumerate(pd.date_range(first, periods=days, freq="D")):
        for block in range(1, BLOCKS + 1):
            rows.append({"delivery_date": day, "block": block,
                         "demand": 200_000 + 100 * offset + block,
                         "wind": 5000 + offset, "solar": 100 * block,
                         "net_demand": 200_000 + 100 * offset + block - (5000 + offset) - 100 * block})
    return pd.DataFrame(rows).set_index(["delivery_date", "block"])


def test_parse_reads_ninety_six_blocks():
    day = pd.Timestamp("2025-06-01")
    frame = parse(sheet(day), day)
    assert len(frame) == BLOCKS
    assert list(frame["block"]) == list(range(1, BLOCKS + 1))
    assert (frame["delivery_date"] == day).all()
    assert frame["demand"].iloc[0] == 200_000


def test_parse_finds_columns_by_name_not_position():
    day = pd.Timestamp("2025-06-01")
    plain = parse(sheet(day), day)
    shifted = parse(sheet(day, extra_column=True), day)
    for field in ("demand", "wind", "solar", "net_demand"):
        assert np.allclose(plain[field], shifted[field])


def test_parse_does_not_confuse_demand_with_net_demand():
    day = pd.Timestamp("2025-06-01")
    frame = parse(sheet(day), day)
    assert (frame["demand"] > frame["net_demand"]).all()
    assert np.allclose(frame["net_demand"], frame["demand"] - frame["wind"] - frame["solar"])


def test_parse_refuses_a_short_sheet():
    day = pd.Timestamp("2025-06-01")
    with pytest.raises(ValueError, match="expected 96"):
        parse(sheet(day, rows=90), day)


def test_parse_refuses_a_sheet_without_the_series():
    book = xlwt.Workbook()
    book.add_sheet("MOP_E").write(0, 0, "nothing useful")
    stream = io.BytesIO()
    book.save(stream)
    with pytest.raises(KeyError, match="no TimeSeries"):
        parse(stream.getvalue(), pd.Timestamp("2024-05-01"))


def test_check_catches_a_doctored_number():
    frame = parse(sheet(pd.Timestamp("2025-06-01")), pd.Timestamp("2025-06-01"))
    frame = frame.set_index(["delivery_date", "block"])
    assert all(worst < 1.0 for _, worst in check(frame))
    frame.iloc[5, frame.columns.get_loc("net_demand")] += 400
    assert any(worst > 1.0 for _, worst in check(frame))


def test_drivers_stop_two_days_before_delivery():
    history = DriverHistory(table(), pd.Timestamp("2025-01-20"))
    assert history.cutoff == pd.Timestamp("2025-01-18")
    assert history.horizon() == 2
    series = history.series("demand")
    assert series.index.max() == pd.Timestamp("2025-01-18")
    assert series.shape[1] == BLOCKS


def test_drivers_refuse_to_return_the_delivery_day():
    history = DriverHistory(table(), pd.Timestamp("2025-01-20"))
    for field in ("demand", "wind", "solar", "net_demand"):
        assert pd.Timestamp("2025-01-19") not in history.series(field).index
        assert pd.Timestamp("2025-01-20") not in history.series(field).index


def test_driver_history_is_blind_to_later_days():
    day = pd.Timestamp("2025-01-20")
    clean = table()
    before = DriverHistory(clean, day).series("demand").to_numpy()

    poisoned = clean.copy()
    later = poisoned.index.get_level_values("delivery_date") >= day - pd.Timedelta(days=1)
    poisoned.loc[later, "demand"] = poisoned.loc[later, "demand"] * 10
    after = DriverHistory(poisoned, day).series("demand").to_numpy()
    assert np.allclose(before, after)


def test_series_tail_gives_the_most_recent_days():
    history = DriverHistory(table(), pd.Timestamp("2025-01-20"))
    recent = history.series("demand", days=7)
    assert len(recent) == 7
    assert recent.index.max() == pd.Timestamp("2025-01-18")
    assert recent.index.min() == pd.Timestamp("2025-01-12")


def test_financial_years_cover_the_range():
    assert years(pd.Timestamp("2024-11-04"), pd.Timestamp("2025-02-01")) == ["2024-25"]
    assert years(pd.Timestamp("2024-11-04"), pd.Timestamp("2026-09-15")) == ["2024-25", "2025-26", "2026-27"]
    assert years(pd.Timestamp("2025-04-01"), pd.Timestamp("2025-04-01")) == ["2025-26"]


def test_listing_prefers_the_path_that_is_actually_served(monkeypatch):
    payload = [
        {"Title_": "04.11.24_NLDC_PSP", "FilePath": "files/grdw/uploads/legacy/04.11.24_NLDC_PSP.xls"},
        {"Title_": "04.11.24_NLDC_PSP", "FilePath": "files/grdw/2024/11/04.11.24_NLDC_PSP_123.xls"},
        {"Title_": "04.11.24_NLDC_PSP", "FilePath": "files/grdw/2024/11/04.11.24_NLDC_PSP_123.pdf"},
    ]
    monkeypatch.setattr("iex.drivers.get", lambda *a, **k: __import__("json").dumps(payload).encode())
    found = listing("2024-25")
    assert found == {pd.Timestamp("2024-11-04"): "files/grdw/2024/11/04.11.24_NLDC_PSP_123.xls"}


def test_series_runs_to_the_cutoff_even_when_the_last_report_is_missing():
    full = table(first="2025-01-01", days=40)
    day = pd.Timestamp("2025-01-20")
    missing = full.drop(index=pd.Timestamp("2025-01-18"), level="delivery_date")
    series = DriverHistory(missing, day).series("demand")
    assert series.index.max() == pd.Timestamp("2025-01-18")
    assert series.notna().all().all()
    # The gap is carried forward from the last day that was published.
    previous = full.loc[pd.Timestamp("2025-01-17")]["demand"].to_numpy()
    assert np.allclose(series.loc[pd.Timestamp("2025-01-18")].to_numpy(), previous)


def test_a_gap_in_the_middle_is_interpolated_between_its_neighbours():
    full = table(first="2025-01-01", days=40)
    day = pd.Timestamp("2025-01-20")
    missing = full.drop(index=pd.Timestamp("2025-01-10"), level="delivery_date")
    series = DriverHistory(missing, day).series("demand")
    before = full.loc[pd.Timestamp("2025-01-09")]["demand"].to_numpy()
    after = full.loc[pd.Timestamp("2025-01-11")]["demand"].to_numpy()
    assert np.allclose(series.loc[pd.Timestamp("2025-01-10")].to_numpy(), (before + after) / 2)


def test_every_day_up_to_the_cutoff_is_present():
    full = table(first="2025-01-01", days=40)
    day = pd.Timestamp("2025-01-25")
    holes = full.drop(index=[pd.Timestamp("2025-01-05"), pd.Timestamp("2025-01-22"),
                             pd.Timestamp("2025-01-23")], level="delivery_date")
    series = DriverHistory(holes, day).series("demand")
    expected = pd.date_range("2025-01-01", "2025-01-23", freq="D")
    assert list(series.index) == list(expected)
