import numpy as np
import pandas as pd
import pytest

from iex.backtest import BLOCKS, History
from iex.driverforecast import FIELDS, HORIZON, LEADS, Drivers, persistence, score
from iex.timesfm import TimesFM
from tests.iex.test_backtest import market
from tests.iex.test_drivers import table as driver_table
from tests.iex.test_timesfm import fake  # noqa: F401


def forecast_table(first="2024-01-01", days=200, fields=("demand", "wind", "solar")):
    rows = []
    for day in pd.date_range(first, periods=days, freq="D"):
        for field in fields:
            for lead in LEADS:
                rows.append(pd.DataFrame({
                    "delivery_date": day, "lead": lead, "block": range(1, BLOCKS + 1),
                    "field": field, "forecast": np.arange(BLOCKS, dtype=float) + 1000 * lead,
                }))
    return pd.concat(rows, ignore_index=True)


@pytest.fixture
def stub_drivers(monkeypatch):
    actuals = driver_table(first="2023-06-01", days=600)
    forecasts = forecast_table(first="2023-06-01", days=600)
    monkeypatch.setattr("iex.driverforecast.load", lambda: actuals)
    monkeypatch.setattr("iex.driverforecast.load_forecasts", lambda: forecasts)
    return actuals, forecasts


def test_covariates_cover_context_plus_delivery_day(stub_drivers):
    table = market(first="2023-06-01", days=600)
    values = Drivers()(History(table, pd.Timestamp("2024-06-01")), context_days=7)
    assert set(values) == {"driver_demand", "driver_wind", "driver_solar"}
    for series in values.values():
        assert series.shape == ((7 + 1) * BLOCKS,)


def test_last_two_days_come_from_the_forecast_not_the_actuals(stub_drivers):
    actuals, _ = stub_drivers
    table = market(first="2023-06-01", days=600)
    day = pd.Timestamp("2024-06-01")
    values = Drivers(fields=("demand",))(History(table, day), context_days=7)
    series = values["driver_demand"]

    # The forecast stub writes lead 1 then lead 2, which are D-1 and D.
    assert np.allclose(series[-2 * BLOCKS: -BLOCKS], (np.arange(BLOCKS) + 1000) / 1000)
    assert np.allclose(series[-BLOCKS:], (np.arange(BLOCKS) + 2000) / 1000)
    # D-2 and earlier are the published actuals.
    settled = actuals.loc[day - pd.Timedelta(days=2)]["demand"].to_numpy() / 1000
    assert np.allclose(series[-3 * BLOCKS: -2 * BLOCKS], settled)


def test_covariates_ignore_driver_actuals_after_the_cutoff(stub_drivers):
    actuals, forecasts = stub_drivers
    table = market(first="2023-06-01", days=600)
    day = pd.Timestamp("2024-06-01")
    before = Drivers()(History(table, day), context_days=7)

    poisoned = actuals.copy()
    later = poisoned.index.get_level_values("delivery_date") >= day - pd.Timedelta(days=1)
    poisoned.loc[later, ["demand", "wind", "solar"]] *= 9
    model = Drivers()
    model.actuals = poisoned
    after = model(History(table, day), context_days=7)
    for field in before:
        assert np.allclose(before[field], after[field])


def test_perfect_drivers_do_read_the_delivery_day(stub_drivers):
    actuals, _ = stub_drivers
    table = market(first="2023-06-01", days=600)
    day = pd.Timestamp("2024-06-01")
    values = Drivers(fields=("demand",), actual=True)(History(table, day), context_days=7)
    expected = actuals.loc[day]["demand"].to_numpy() / 1000
    assert np.allclose(values["driver_demand"][-BLOCKS:], expected)


def test_a_missing_forecast_is_refused_rather_than_filled(stub_drivers):
    _, forecasts = stub_drivers
    table = market(first="2023-06-01", days=600)
    day = pd.Timestamp("2024-06-01")
    model = Drivers()
    model.forecasts = forecasts[forecasts["delivery_date"] != day]
    with pytest.raises(ValueError, match="no driver forecast"):
        model(History(table, day), context_days=7)


def test_a_truncated_forecast_is_refused(stub_drivers):
    _, forecasts = stub_drivers
    table = market(first="2023-06-01", days=600)
    day = pd.Timestamp("2024-06-01")
    model = Drivers(fields=("demand",))
    keep = ~((forecasts["delivery_date"] == day) & (forecasts["block"] > 50))
    model.forecasts = forecasts[keep]
    with pytest.raises(ValueError, match="blocks"):
        model(History(table, day), context_days=7)


def test_unknown_driver_is_refused(stub_drivers):
    with pytest.raises(ValueError, match="carry no"):
        Drivers(fields=("unobtainium",))


def test_driver_covariates_reach_the_model(stub_drivers, fake):  # noqa: F811
    table = market(first="2023-06-01", days=600)
    TimesFM(context_days=7, weather=Drivers())(History(table, pd.Timestamp("2024-06-01")))
    future = fake.calls[0]["past_future_covariates"]
    assert future.shape == (3, (7 + 1) * BLOCKS)


def test_score_lines_each_lead_up_with_the_day_it_predicted():
    actuals = driver_table(first="2024-01-01", days=60)
    forecasts = forecast_table(first="2024-01-01", days=60, fields=("demand",))
    board = score(forecasts, actuals, fields=("demand",))
    assert set(board["lead_days"]) == set(LEADS)
    assert (board["days"] > 0).all()


def test_persistence_scores_both_leads():
    actuals = driver_table(first="2024-01-01", days=60)
    board = persistence(actuals, fields=("demand", "wind"))
    assert len(board) == len(LEADS) * 2
    # The series climbs by a fixed step each day, so copying an older day is worse.
    demand = board[board["field"] == "demand"].set_index("lead_days")["mae"]
    assert demand[1] > demand[2]


def test_horizon_is_two_whole_days():
    assert HORIZON == 2 * BLOCKS
    assert FIELDS[:3] == ("demand", "wind", "solar")


def test_a_gap_in_the_published_days_is_filled_not_fatal(stub_drivers):
    actuals, _ = stub_drivers
    table = market(first="2023-06-01", days=600)
    day = pd.Timestamp("2024-06-01")
    model = Drivers(fields=("demand",))
    model.actuals = actuals.drop(index=pd.Timestamp("2024-05-27"), level="delivery_date")
    model.usable = {d: True for d in actuals.index.get_level_values("delivery_date").unique()}
    values = model(History(table, day), context_days=7)
    assert values["driver_demand"].shape == ((7 + 1) * BLOCKS,)
    assert np.isfinite(values["driver_demand"]).all()


def test_an_unusable_day_is_filled_from_earlier_days_only(stub_drivers):
    actuals, _ = stub_drivers
    table = market(first="2023-06-01", days=600)
    day = pd.Timestamp("2024-06-01")
    broken = pd.Timestamp("2024-05-28")
    model = Drivers(fields=("demand",))
    model.usable = {d: d != broken for d in actuals.index.get_level_values("delivery_date").unique()}
    before = model(History(table, day), context_days=7)["driver_demand"]

    # Whatever nonsense the broken day carries must not reach the covariate.
    poisoned = actuals.copy()
    poisoned.loc[poisoned.index.get_level_values("delivery_date") == broken, "demand"] = 9e9
    model.actuals = poisoned
    after = model(History(table, day), context_days=7)["driver_demand"]
    assert np.allclose(before, after)


def test_perfect_drivers_refuse_an_unusable_delivery_day(stub_drivers):
    actuals, _ = stub_drivers
    table = market(first="2023-06-01", days=600)
    day = pd.Timestamp("2024-06-01")
    model = Drivers(fields=("demand",), actual=True)
    model.usable = {d: d != day for d in actuals.index.get_level_values("delivery_date").unique()}
    with pytest.raises(ValueError, match="observed drivers unavailable"):
        model(History(table, day), context_days=7)
