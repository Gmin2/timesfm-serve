import pandas as pd
import pytest

from iex.operator import ROW, _years, parse

LETTER = """
                                                        National Load Despatch Centre
As per article 31.2(i) of the Indian Electricity Grid Code 2023, the daily all-India
demand forecasting error is as given below.

                  For Date : 08-Sep-2026
                                          Absolute Error (%)
                                                    Day Ahead        Real time
                             Demand Met                3.3%            0.8%
                         Energy Consumption            3.3%            0.2%
                  Note:
                  1).Error for demand met is Mean Absolute Percentage Error (MAPE).
"""


def test_reads_both_percentages():
    row = parse(LETTER, pd.Timestamp("2026-09-08"))
    assert row["day_ahead_mape"] == 3.3
    assert row["real_time_mape"] == 0.8


def test_trusts_the_date_the_letter_states_over_the_one_it_is_filed_under():
    row = parse(LETTER, pd.Timestamp("2026-09-09"))
    assert row["delivery_date"] == pd.Timestamp("2026-09-08")
    assert row["listed_as"] == pd.Timestamp("2026-09-09")


def test_takes_the_demand_row_not_the_energy_row():
    swapped = LETTER.replace("Energy Consumption            3.3%            0.2%",
                             "Energy Consumption            9.9%            9.9%")
    row = parse(swapped, pd.Timestamp("2026-09-08"))
    assert row["day_ahead_mape"] == 3.3


def test_refuses_a_letter_with_no_date():
    with pytest.raises(ValueError, match="no reporting date"):
        parse(LETTER.replace("For Date : 08-Sep-2026", ""), pd.Timestamp("2026-09-08"))


def test_refuses_a_letter_with_no_demand_row():
    with pytest.raises(ValueError, match="no demand met row"):
        parse(LETTER.replace("Demand Met                3.3%            0.8%", ""),
              pd.Timestamp("2026-09-08"))


def test_row_pattern_survives_a_tighter_layout():
    assert ROW.search("Demand Met 12.5% 1.0%").groups() == ("12.5", "1.0")


def test_financial_years_cover_the_range():
    assert _years(pd.Timestamp("2025-02-01"), pd.Timestamp("2026-09-15")) == ["2024-25", "2025-26", "2026-27"]
