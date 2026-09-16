from datetime import date

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


def test_wind_sites_are_weighted_and_sum_to_one():
    from iex.weather import WIND_SITES

    weights = [weight for _, _, weight in WIND_SITES.values()]
    assert sum(weights) == pytest.approx(1.0, abs=0.01)
    assert all(0 < weight < 0.5 for weight in weights)
    # Nothing in the east, where Grid-India records no wind generation at all.
    assert all(longitude < 82 for _, (_, longitude, _) in
               [(name, site) for name, site in WIND_SITES.items()])


def test_wind_and_demand_sample_different_places():
    from iex.weather import CITIES, WIND_SITES

    assert not set(CITIES) & set(WIND_SITES)


def test_the_demand_group_keeps_its_original_cache_filenames(monkeypatch, tmp_path):
    from iex import weather

    monkeypatch.setattr(weather, "STORE", tmp_path)
    (tmp_path / "forecast").mkdir()
    (tmp_path / "forecast" / "delhi_20240101_20240102.json").write_text('{"hourly": {}}')

    def explode(*args, **kwargs):
        raise AssertionError("a cached demand file was re-downloaded")

    monkeypatch.setattr(weather.requests, "get", explode)
    assert weather.fetch("delhi", date(2024, 1, 1), date(2024, 1, 2)) == {"hourly": {}}


def test_a_new_group_gets_its_own_cache_filenames(monkeypatch, tmp_path):
    from iex import weather

    monkeypatch.setattr(weather, "STORE", tmp_path)
    (tmp_path / "forecast").mkdir()
    (tmp_path / "forecast" / "wind_kutch_20240101_20240102.json").write_text('{"hourly": {"ok": 1}}')
    got = weather.fetch("kutch", date(2024, 1, 1), date(2024, 1, 2), group="wind")
    assert got == {"hourly": {"ok": 1}}


def test_wind_speed_is_cubed_before_weighting(monkeypatch):
    from iex import weather

    times = pd.date_range("2025-01-01", periods=24, freq="h")
    speeds = {"kutch": 10.0, "muppandal": 20.0, "chitradurga": 0.0, "satara": 0.0,
              "anantapur": 0.0, "jaisalmer": 0.0, "dewas": 0.0}
    monkeypatch.setattr(weather, "fetch", lambda city, *a, **k: {
        "hourly": {"time": [t.isoformat() for t in times],
                   "wind_speed_100m_previous_day2": [speeds[city]] * 24}})

    got = weather.national(date(2025, 1, 1), date(2025, 1, 1), group="wind")
    expected = 0.281 * 10.0 ** 3 + 0.227 * 20.0 ** 3
    assert got["wind_speed_100m"].iloc[0] == pytest.approx(expected, rel=1e-6)
    # The cube of the weighted mean would be far smaller, which is the whole point.
    assert got["wind_speed_100m"].iloc[0] > (0.281 * 10 + 0.227 * 20) ** 3
