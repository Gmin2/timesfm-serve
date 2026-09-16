import numpy as np
import pandas as pd
import pytest

from iex.backtest import BLOCKS, History, LeakageError
from iex.lear import HOURS, LAGS, Lear, features, fit_window, hourly, invert, scaling, transform
from tests.iex.test_backtest import market


def series(days=420, seed=1):
    """Daily prices with a weekly shape, so weekday indicators carry real signal."""
    generator = np.random.default_rng(seed)
    blocks = np.arange(BLOCKS)
    shape = 2000 + 800 * np.sin(2 * np.pi * blocks / BLOCKS)
    prices = np.empty((days, BLOCKS))
    weekdays = np.empty(days, dtype=int)
    for day in range(days):
        weekday = day % 7
        weekdays[day] = weekday
        weekend = 0.8 if weekday >= 5 else 1.0
        prices[day] = shape * weekend + generator.normal(0, 40, BLOCKS)
    return prices, weekdays


def test_scaling_and_transform_round_trip():
    values = np.array([[1.0, 100.0], [2.0, 200.0], [3.0, 300.0], [4.0, 400.0]])
    centre, spread = scaling(values)
    assert np.allclose(invert(transform(values, centre, spread), centre, spread), values)
    # A constant column would divide by zero, so the spread falls back to one.
    _, flat = scaling(np.ones((5, 2)))
    assert np.all(flat == 1.0)


def test_features_use_only_the_declared_lags():
    prices, weekdays = series(days=30)
    row = features(prices, weekdays, 20)
    # Yesterday keeps all 96 blocks; the older lags are reduced to hourly means.
    assert row.shape == (BLOCKS + 3 * HOURS + 7,)
    assert np.array_equal(row[:BLOCKS], prices[19])
    for position, lag in enumerate(LAGS[1:]):
        start = BLOCKS + position * HOURS
        assert np.allclose(row[start:start + HOURS], hourly(prices[20 - lag]))
    assert row[-7:].sum() == 1.0  # exactly one weekday indicator is set


def test_hourly_means_average_four_blocks_each():
    blocks = np.arange(BLOCKS, dtype=float)
    means = hourly(blocks)
    assert means.shape == (HOURS,)
    assert means[0] == pytest.approx(1.5)   # blocks 0..3
    assert means[-1] == pytest.approx(93.5)  # blocks 92..95


def test_features_never_read_the_target_day():
    prices, weekdays = series(days=30)
    before = features(prices, weekdays, 20)
    poisoned = prices.copy()
    poisoned[20] = 99_999.0
    assert np.array_equal(before, features(poisoned, weekdays, 20))


def test_fit_window_beats_a_flat_guess_on_a_weekly_pattern():
    prices, weekdays = series()
    target = 400
    forecast, alphas = fit_window(prices, weekdays, target, window=120)
    assert forecast.shape == (BLOCKS,) and np.isfinite(forecast).all()
    assert alphas.shape == (BLOCKS,) and (alphas > 0).all()
    lear_error = np.abs(forecast - prices[target]).mean()
    flat_error = np.abs(prices[target - 120:target].mean() - prices[target]).mean()
    assert lear_error < flat_error


def test_fit_window_refuses_when_history_is_too_short():
    prices, weekdays = series(days=40)
    with pytest.raises(ValueError, match="need"):
        fit_window(prices, weekdays, target=30, window=60)


def test_reused_penalties_match_a_fresh_fit_when_nothing_changed():
    prices, weekdays = series()
    fresh, alphas = fit_window(prices, weekdays, 400, window=120)
    reused, _ = fit_window(prices, weekdays, 400, window=120, seed_alpha=alphas)
    assert np.allclose(fresh, reused)


def test_lear_forecaster_respects_the_information_cutoff():
    table = market(first="2023-06-01", days=500)
    model = Lear(windows=(56,), reselect_every=1)
    day = pd.Timestamp("2024-06-01")
    before = model(History(table, day))

    scrambled = table.copy()
    unknowable = scrambled["delivery_date"] >= day
    scrambled.loc[unknowable, "price"] = scrambled.loc[unknowable, "price"] * 3 + 500
    after = Lear(windows=(56,), reselect_every=1)(History(scrambled, day))
    assert np.allclose(before, after)


def test_lear_averages_its_calibration_windows():
    table = market(first="2023-06-01", days=500)
    day = pd.Timestamp("2024-06-01")
    single = {w: Lear(windows=(w,), reselect_every=1)(History(table, day)) for w in (56, 84)}
    both = Lear(windows=(56, 84), reselect_every=1)(History(table, day))
    assert np.allclose(both, np.mean(list(single.values()), axis=0))


def test_lear_skips_windows_longer_than_the_history_it_has():
    table = market(first="2024-01-01", days=200)
    # The 728-day window cannot be built here, but the 56-day one can.
    forecast = Lear(windows=(56, 728), reselect_every=1)(History(table, pd.Timestamp("2024-04-10")))
    assert forecast.shape == (BLOCKS,) and np.isfinite(forecast).all()

    with pytest.raises(ValueError, match="not enough history"):
        Lear(windows=(728,), reselect_every=1)(History(table, pd.Timestamp("2024-04-10")))


def test_history_guard_still_applies_through_lear():
    class BrokenFilter(History):
        def _upto_cutoff(self, market_name):
            return self._table[self._table["market"] == market_name]

    table = market(first="2024-01-01", days=200)
    with pytest.raises(LeakageError):
        Lear(windows=(56,), reselect_every=1)(BrokenFilter(table, pd.Timestamp("2024-04-10")))
