import numpy as np
import pandas as pd
import pytest

from iex.backtest import (
    BLOCKS,
    PERIODS,
    History,
    LeakageError,
    daily_losses,
    diebold_mariano,
    naive_last_week,
    naive_yesterday,
    run,
    score,
)


def market(first="2024-01-01", days=200, market="dam", seed=0):
    """A synthetic market whose price rises every day, so leakage is obvious."""
    generator = np.random.default_rng(seed)
    rows = []
    for offset, day in enumerate(pd.date_range(first, periods=days, freq="D")):
        base = 1000 + 10 * offset
        for block in range(1, BLOCKS + 1):
            rows.append({
                "market": market, "delivery_date": day, "block": block,
                "price": base + block + generator.normal(0, 5),
                "usable": True, "at_cap": False, "complete_day": True, "cap": 10_000.0,
            })
    return pd.DataFrame(rows)


def test_history_stops_at_the_cutoff():
    table = market()
    history = History(table, pd.Timestamp("2024-03-20"))
    prices = history.prices()
    assert prices.index.max() == pd.Timestamp("2024-03-19")
    assert pd.Timestamp("2024-03-20") not in prices.index
    assert history.cutoff == pd.Timestamp("2024-03-19")
    assert len(history.last_day()) == BLOCKS


def test_history_raises_rather_than_leaking():
    """If filtering ever fails, History must raise instead of returning the future."""
    class BrokenFilter(History):
        def _upto_cutoff(self, market):  # the shape a filtering bug would take
            return self._table[self._table["market"] == market]

    with pytest.raises(LeakageError, match="past cutoff"):
        BrokenFilter(market(), pd.Timestamp("2024-03-20")).prices()


def test_a_cheating_forecaster_is_caught_by_the_scramble_test():
    """The main rule, as a test: nothing after the cutoff may change a forecast."""
    table = market()

    def honest(history):
        return history.last_day()

    def cheat(history):
        # Reaches around History into the future of the table it was handed.
        raw = history._table
        future = raw[raw["delivery_date"] == history.delivery_date]
        return future.sort_values("block")["price"].to_numpy()

    for name, forecaster in (("honest", honest), ("cheat", cheat)):
        changed = False
        for day in pd.date_range("2024-04-02", periods=10, freq="D"):
            before = forecaster(History(table, day))
            # Everything from the delivery day onward is unknowable at 09:30 on D-1.
            scrambled = table.copy()
            unknowable = scrambled["delivery_date"] >= day
            scrambled.loc[unknowable, "price"] = scrambled.loc[unknowable, "price"] * 3 + 500
            after = forecaster(History(scrambled, day))
            changed |= not np.allclose(before, after)
        assert changed == (name == "cheat"), f"{name}: forecast changed = {changed}"


def test_run_scores_only_complete_usable_days():
    table = market()
    table.loc[table["delivery_date"] == pd.Timestamp("2024-04-05"), "usable"] = False
    results = run(table, {"naive_yesterday": naive_yesterday}, period="development", limit=15)
    assert pd.Timestamp("2024-04-05") not in set(results["delivery_date"])
    assert set(results["block"]) == set(range(1, BLOCKS + 1))


def test_run_rejects_a_wrong_shaped_forecast():
    with pytest.raises(ValueError, match="expected"):
        run(market(), {"stub": lambda h: np.zeros(24)}, period="development", limit=3)


def test_forecasts_are_clipped_to_the_price_cap():
    results = run(market(), {"huge": lambda h: np.full(BLOCKS, 50_000.0)}, period="development", limit=3)
    assert results["forecast"].max() == 10_000.0


def test_score_reports_mae_and_relative_error():
    table = market()
    results = run(table, {"naive_yesterday": naive_yesterday, "naive_last_week": naive_last_week},
                  period="development", limit=40)
    table_out = score(results)
    assert set(table_out.index) == {"naive_yesterday", "naive_last_week"}
    assert table_out.loc["naive_yesterday", "rmae"] == pytest.approx(1.0)
    # Prices drift upward here, so yesterday must beat last week.
    assert table_out.loc["naive_yesterday", "mae"] < table_out.loc["naive_last_week", "mae"]


def test_diebold_mariano_detects_a_real_difference_and_ignores_a_tie():
    table = market()
    results = run(table, {"naive_yesterday": naive_yesterday, "naive_last_week": naive_last_week},
                  period="development", limit=60)
    losses = daily_losses(results)
    verdict = diebold_mariano(losses, "naive_yesterday", "naive_last_week")
    assert verdict["better"] and verdict["p_value"] < 0.01

    tied = losses.assign(copy=losses["naive_yesterday"])
    assert diebold_mariano(tied, "copy", "naive_yesterday")["p_value"] == pytest.approx(1.0)


def test_evaluation_periods_do_not_overlap():
    spans = sorted(PERIODS.values())
    for (_, earlier_end), (later_start, _) in zip(spans, spans[1:], strict=False):
        assert earlier_end < later_start
