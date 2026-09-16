import numpy as np
import pandas as pd
import pytest

from iex.backtest import BLOCKS, History, LeakageError
from iex.timesfm import MAX_CONTEXT, TimesFM
from tests.iex.test_backtest import market


class FakeOutput:
    def __init__(self, forecast, quantiles):
        self.forecast = forecast
        self.quantiles = quantiles


class FakeModel:
    """Records what it was handed, and echoes the last day of the context back."""

    def __init__(self):
        self.calls = []

    def predict(self, context, horizon, **options):
        self.calls.append({"context": np.asarray(context), "horizon": horizon, **options})
        tail = np.asarray(context)[-horizon:]
        return FakeOutput(tail, np.tile(tail[:, None], (1, 9)))


@pytest.fixture
def fake(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr("iex.timesfm.session", lambda cache_dir=None: (model, "cpu"))
    return model


def test_context_is_one_flat_series_ending_at_the_cutoff(fake):
    table = market(first="2024-01-01", days=200)
    day = pd.Timestamp("2024-04-10")
    TimesFM(context_days=7)(History(table, day))

    context = fake.calls[0]["context"]
    assert context.shape == (7 * BLOCKS,)
    yesterday = table[table["delivery_date"] == day - pd.Timedelta(days=1)].sort_values("block")["price"]
    assert np.allclose(context[-BLOCKS:], yesterday.to_numpy())


def test_horizon_is_always_one_delivery_day(fake):
    table = market(first="2024-01-01", days=200)
    forecast = TimesFM(context_days=7)(History(table, pd.Timestamp("2024-04-10")))
    assert fake.calls[0]["horizon"] == BLOCKS
    assert forecast.shape == (BLOCKS,)


def test_context_is_capped_at_the_models_limit(fake):
    table = market(first="2022-01-01", days=900)
    TimesFM(context_days=400)(History(table, pd.Timestamp("2024-04-10")))
    assert fake.calls[0]["context"].shape == (MAX_CONTEXT,)


def test_log_transform_round_trips(fake):
    table = market(first="2024-01-01", days=200)
    day = pd.Timestamp("2024-04-10")
    raw = TimesFM(context_days=7)(History(table, day))
    logged = TimesFM(context_days=7, log=True)(History(table, day))
    # The fake echoes its input, so log1p in and expm1 out must return the same prices.
    assert np.allclose(raw, logged)
    assert np.allclose(fake.calls[1]["context"], np.log1p(fake.calls[0]["context"]))


def test_positivity_is_requested_only_without_the_log_transform(fake):
    table = market(first="2024-01-01", days=200)
    day = pd.Timestamp("2024-04-10")
    TimesFM(context_days=7)(History(table, day))
    TimesFM(context_days=7, log=True)(History(table, day))
    assert fake.calls[0]["make_positive"] is True
    assert fake.calls[1]["make_positive"] is False


def test_quantiles_are_kept_for_every_block(fake):
    table = market(first="2024-01-01", days=200)
    model = TimesFM(context_days=7)
    model(History(table, pd.Timestamp("2024-04-10")))
    assert model.quantiles.shape == (BLOCKS, 9)


def test_forecast_never_sees_past_the_cutoff(fake):
    table = market(first="2024-01-01", days=200)
    day = pd.Timestamp("2024-04-10")
    before = TimesFM(context_days=7)(History(table, day))

    scrambled = table.copy()
    unknowable = scrambled["delivery_date"] >= day
    scrambled.loc[unknowable, "price"] = scrambled.loc[unknowable, "price"] * 3 + 500
    after = TimesFM(context_days=7)(History(scrambled, day))
    assert np.allclose(before, after)


def test_history_guard_still_applies(fake):
    class BrokenFilter(History):
        def _upto_cutoff(self, market_name):
            return self._table[self._table["market"] == market_name]

    table = market(first="2024-01-01", days=200)
    with pytest.raises(LeakageError):
        TimesFM(context_days=7)(BrokenFilter(table, pd.Timestamp("2024-04-10")))
