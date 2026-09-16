import numpy as np
import pandas as pd
import pytest

from iex.backtest import BLOCKS, History
from iex.timesfm import TimesFM
from iex.weather import CITIES, VARIABLES, Weather, to_blocks
from tests.iex.test_backtest import market
from tests.iex.test_timesfm import fake  # noqa: F401


def weather_table(first="2023-06-01", days=600):
    days_index = pd.date_range(first, periods=days, freq="D")
    index = pd.MultiIndex.from_product([days_index, range(1, BLOCKS + 1)],
                                       names=["delivery_date", "block"])
    generator = np.random.default_rng(3)
    return pd.DataFrame(
        {"temperature_2m": generator.normal(28, 4, len(index)),
         "shortwave_radiation": generator.uniform(0, 900, len(index))},
        index=index)


@pytest.fixture
def stub_weather(monkeypatch):
    table = weather_table()
    monkeypatch.setattr("iex.weather.load", lambda actual=False: table)
    return table


def test_city_weights_are_sensible():
    weights = [weight for _, _, weight in CITIES.values()]
    assert len(CITIES) >= 6
    assert all(0 < weight < 0.5 for weight in weights)
    assert sum(weights) == pytest.approx(1.0, abs=0.01)


def test_hourly_values_expand_to_blocks():
    times = pd.date_range("2024-11-11", periods=48, freq="h")
    hourly = pd.DataFrame({name: np.arange(48, dtype=float) for name in VARIABLES}, index=times)
    blocks = to_blocks(hourly)
    assert len(blocks) == 2 * BLOCKS
    assert blocks.index.get_level_values("block").max() == BLOCKS
    # Each hour covers four consecutive blocks with the same value.
    first_hour = blocks.xs(pd.Timestamp("2024-11-11"), level="delivery_date").head(4)
    assert first_hour["temperature_2m"].nunique() == 1


def test_weather_covers_context_plus_delivery_day(stub_weather):
    table = market(first="2023-06-01", days=600)
    values = Weather()(History(table, pd.Timestamp("2024-06-01")), context_days=7)
    assert set(values) == set(VARIABLES)
    for series in values.values():
        assert series.shape == ((7 + 1) * BLOCKS,)


def test_missing_weather_is_refused_rather_than_filled(stub_weather):
    table = market(first="2023-06-01", days=600)
    trimmed = stub_weather.drop(index=pd.Timestamp("2024-06-01"), level="delivery_date")
    weather = Weather()
    weather.table = trimmed
    with pytest.raises(ValueError, match="weather missing"):
        weather(History(table, pd.Timestamp("2024-06-01")), context_days=7)


def test_weather_values_line_up_with_the_delivery_day(stub_weather):
    table = market(first="2023-06-01", days=600)
    day = pd.Timestamp("2024-06-01")
    values = Weather()(History(table, day), context_days=7)
    expected = stub_weather.loc[day]["temperature_2m"].to_numpy()
    assert np.allclose(values["temperature_2m"][-BLOCKS:], expected)


def test_weather_covariates_reach_the_model(stub_weather, fake):  # noqa: F811
    table = market(first="2023-06-01", days=600)
    TimesFM(context_days=7, weather=Weather())(History(table, pd.Timestamp("2024-06-01")))
    future = fake.calls[0]["past_future_covariates"]
    assert future.shape == (len(VARIABLES), (7 + 1) * BLOCKS)


def test_calendar_and_weather_stack_together(stub_weather, fake):  # noqa: F811
    table = market(first="2023-06-01", days=600)
    TimesFM(context_days=7, use_calendar=True, weather=Weather())(
        History(table, pd.Timestamp("2024-06-01")))
    future = fake.calls[0]["past_future_covariates"]
    assert future.shape == (4 + len(VARIABLES), (7 + 1) * BLOCKS)


def test_weather_forecast_is_unaffected_by_scrambling_prices(stub_weather, fake):  # noqa: F811
    table = market(first="2023-06-01", days=600)
    day = pd.Timestamp("2024-06-01")
    model = TimesFM(context_days=7, use_calendar=True, weather=Weather())
    before = model(History(table, day))

    scrambled = table.copy()
    later = scrambled["delivery_date"] >= day
    scrambled.loc[later, "price"] = scrambled.loc[later, "price"] * 3 + 500
    assert np.allclose(before, model(History(scrambled, day)))
