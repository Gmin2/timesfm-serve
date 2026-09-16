import numpy as np
import pandas as pd
import pytest

from iex.backtest import BLOCKS, History
from iex.covariates import bid_ratio, calendar, future_calendar, is_holiday, past_bid_ratio
from iex.timesfm import TimesFM
from tests.iex.test_backtest import market
from tests.iex.test_timesfm import fake  # noqa: F401


def test_calendar_shapes_and_block_cycle():
    days = pd.date_range("2024-11-11", periods=3, freq="D")
    values = calendar(days)
    assert set(values) == {"block_sin", "block_cos", "weekend", "holiday"}
    for series in values.values():
        assert series.shape == (3 * BLOCKS,)
    # Block of day is circular, so the last block sits next to the first.
    first, last = values["block_sin"][0], values["block_sin"][BLOCKS - 1]
    assert abs(first - last) < 0.1
    assert np.isclose(values["block_sin"][:BLOCKS] ** 2 + values["block_cos"][:BLOCKS] ** 2, 1.0).all()


def test_calendar_marks_weekends_and_holidays():
    values = calendar([pd.Timestamp("2024-11-16")])  # a Saturday
    assert values["weekend"].max() == 1.0
    values = calendar([pd.Timestamp("2024-11-14")])  # a Thursday
    assert values["weekend"].max() == 0.0
    assert is_holiday("2024-10-31")   # Diwali
    assert not is_holiday("2024-10-30")


def test_bid_ratio_is_sell_over_purchase():
    table = market(first="2024-01-01", days=40)
    table["sell_bid"] = 200.0
    table["purchase_bid"] = 100.0
    ratio = bid_ratio(History(table, pd.Timestamp("2024-02-01")))
    assert np.allclose(ratio.to_numpy(), 2.0)
    assert ratio.index.max() == pd.Timestamp("2024-01-31")


def test_bid_ratio_survives_a_zero_denominator():
    table = market(first="2024-01-01", days=40)
    table["sell_bid"] = 200.0
    table["purchase_bid"] = 100.0
    table.loc[table["delivery_date"] == pd.Timestamp("2024-01-20"), "purchase_bid"] = 0.0
    flat = past_bid_ratio(History(table, pd.Timestamp("2024-02-01")), context_days=30)
    assert np.isfinite(flat).all()


def test_future_calendar_covers_the_context_plus_the_delivery_day():
    table = market(first="2024-01-01", days=200)
    history = History(table, pd.Timestamp("2024-04-10"))
    values = future_calendar(history, context_days=7)
    for series in values.values():
        assert series.shape == ((7 + 1) * BLOCKS,)


def test_past_bid_ratio_stops_at_the_cutoff():
    table = market(first="2024-01-01", days=200)
    table["sell_bid"] = 200.0
    table["purchase_bid"] = 100.0
    day = pd.Timestamp("2024-04-10")
    flat = past_bid_ratio(History(table, day), context_days=7)
    assert flat.shape == (7 * BLOCKS,)

    poisoned = table.copy()
    later = poisoned["delivery_date"] >= day
    poisoned.loc[later, "sell_bid"] = 99_999.0
    assert np.allclose(flat, past_bid_ratio(History(poisoned, day), context_days=7))


def test_future_covariates_are_context_plus_horizon_long(fake):  # noqa: F811
    table = market(first="2024-01-01", days=200)
    TimesFM(context_days=7, use_calendar=True)(History(table, pd.Timestamp("2024-04-10")))
    future = fake.calls[0]["past_future_covariates"]
    assert future.shape == (4, (7 + 1) * BLOCKS)


def test_past_covariates_stop_at_the_context(fake):  # noqa: F811
    table = market(first="2024-01-01", days=200)
    table["sell_bid"] = 200.0
    table["purchase_bid"] = 100.0
    TimesFM(context_days=7, use_bids=True)(History(table, pd.Timestamp("2024-04-10")))
    past = fake.calls[0]["past_only_covariates"]
    assert past.shape == (1, 7 * BLOCKS)


def test_no_covariates_are_passed_when_not_requested(fake):  # noqa: F811
    table = market(first="2024-01-01", days=200)
    TimesFM(context_days=7)(History(table, pd.Timestamp("2024-04-10")))
    assert fake.calls[0]["past_future_covariates"] is None
    assert fake.calls[0]["past_only_covariates"] is None


def test_covariate_forecasts_ignore_everything_after_the_cutoff(fake):  # noqa: F811
    table = market(first="2024-01-01", days=200)
    table["sell_bid"] = 200.0
    table["purchase_bid"] = 100.0
    day = pd.Timestamp("2024-04-10")
    model = TimesFM(context_days=7, use_calendar=True, use_bids=True)
    before = model(History(table, day))

    scrambled = table.copy()
    later = scrambled["delivery_date"] >= day
    scrambled.loc[later, ["price", "sell_bid", "purchase_bid"]] *= 3
    assert np.allclose(before, model(History(scrambled, day)))


def test_a_wrong_length_covariate_is_rejected(fake, monkeypatch):  # noqa: F811
    table = market(first="2024-01-01", days=200)
    monkeypatch.setattr("iex.covariates.future_calendar",
                        lambda history, context_days: {"bad": np.zeros(5)})
    with pytest.raises(ValueError, match="future covariates"):
        TimesFM(context_days=7, use_calendar=True)(History(table, pd.Timestamp("2024-04-10")))
