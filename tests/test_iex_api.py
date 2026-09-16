"""The IEX half of the shared gateway, against a real database."""

import uuid
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from iex import store
from iex.backtest import BLOCKS
from iex.publish import CAP, IST, cutoff_for
from timesfm_serve import db
from timesfm_serve.api import app


@pytest.fixture(scope="session", autouse=True)
def schema():
    db.migrate()
    yield
    db.pool.close()


@pytest.fixture
def customer():
    account = db.find_or_create_account(f"iex-test-{uuid.uuid4()}")
    with db.conn() as c:
        c.execute("update accounts set rate_limit_per_min = 10000 where id = %s", (account,))
    key = db.create_key(account)
    yield {"x-api-key": key}
    with db.conn() as c:
        c.execute("delete from rate_limit_counters where key_hash in"
                  " (select hash from api_keys where account_id = %s)", (account,))
        c.execute("delete from accounts where id = %s", (account,))


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def issued():
    """One stored forecast on a day far from any other test's day."""
    day = date(2031, 3, 14)
    forecast = np.linspace(1500, 9000, BLOCKS)
    quantiles = np.stack([forecast - 500, forecast - 400, forecast - 300, forecast - 150,
                          forecast, forecast + 150, forecast + 300, forecast + 400,
                          forecast + 500], axis=1)
    with db.conn() as c:
        c.execute("delete from iex_forecasts where delivery_date = %s", (day,))
        identifier = store.record(c, day, "test_model", cutoff_for(day), forecast,
                                  quantiles, "deadbeef")
    yield day, identifier, forecast
    with db.conn() as c:
        c.execute("delete from iex_forecasts where delivery_date = %s", (day,))


def test_both_applications_are_served_by_one_gateway(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert any(p.startswith("/v1/iex/") for p in paths)
    assert any(p.startswith("/v1/weather/") for p in paths)
    # and they share one authentication surface
    assert "/v1/account/keys" in paths


def test_a_stored_forecast_comes_back_whole(client, customer, issued):
    day, _, forecast = issued
    body = client.get(f"/v1/iex/forecast/{day.isoformat()}", headers=customer).json()
    assert body["delivery_date"] == day.isoformat()
    assert len(body["blocks"]) == BLOCKS
    assert body["blocks"][0]["block"] == 1
    assert body["blocks"][-1]["forecast"] == pytest.approx(forecast[-1], abs=0.01)
    assert body["model_revision"] == "deadbeef"


def test_quantiles_survive_the_round_trip(client, customer, issued):
    day, _, _ = issued
    block = client.get(f"/v1/iex/forecast/{day.isoformat()}", headers=customer).json()["blocks"][10]
    levels = [block[f"q{n}"] for n in (10, 20, 30, 40, 50, 60, 70, 80, 90)]
    assert levels == sorted(levels)
    assert block["q10"] < block["forecast"] < block["q90"]


def test_forecasts_are_never_overwritten(issued):
    day, identifier, _ = issued
    with db.conn() as c:
        again = store.record(c, day, "test_model", cutoff_for(day),
                             np.full(BLOCKS, 9999.0), None, "different")
        kept = store.for_day(c, day, "test_model")
    assert again is None
    assert kept["id"] == identifier
    assert kept["revision"] == "deadbeef"


def test_a_day_we_never_issued_is_a_404(client, customer):
    response = client.get("/v1/iex/forecast/2011-01-01", headers=customer)
    assert response.status_code == 404


def test_every_route_needs_a_key(client):
    for path in ("/v1/iex/forecast/latest", "/v1/iex/forecast/2031-03-14", "/v1/iex/scorecard"):
        assert client.get(path).status_code == 401


def test_the_cutoff_is_half_past_nine_the_morning_before():
    cutoff = cutoff_for(date(2026, 9, 15))
    assert cutoff.date() == date(2026, 9, 14)
    assert (cutoff.hour, cutoff.minute) == (9, 30)
    assert cutoff.utcoffset() == timedelta(hours=5, minutes=30)
    # which is 04:00 UTC, comfortably before bidding opens at 10:00 IST
    assert cutoff.astimezone(timezone.utc).hour == 4


def test_the_cap_matches_the_regulated_ceiling():
    assert CAP == 10_000.0
    assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)


def test_the_store_refuses_a_wrong_length_day():
    with db.conn() as c:
        with pytest.raises(ValueError, match="expected 96 blocks"):
            store.record(c, date(2031, 4, 1), "short", datetime.now(IST), np.zeros(50))


def test_the_scorecard_reports_what_has_been_scored(client, customer, issued):
    day, identifier, _ = issued
    with db.conn() as c:
        store.record_score(c, identifier, {"blocks_scored": BLOCKS, "mae": 412.5,
                                           "rmse": 600.1, "bias": -12.0,
                                           "coverage_p10_p90": 0.81})
    body = client.get("/v1/iex/scorecard", headers=customer).json()
    mine = [row for row in body["days"] if row["delivery_date"] == day.isoformat()]
    assert mine and mine[0]["mae"] == pytest.approx(412.5)
    assert body["summary"]["days"] >= 1


def test_the_licence_notice_travels_with_the_data(client, customer, issued):
    day, _, _ = issued
    body = client.get(f"/v1/iex/forecast/{day.isoformat()}", headers=customer).json()
    assert "non-commercial" in body["notice"]
    # our forecasts are published; the exchange's own prices are not
    assert not any("actual" in key for block in body["blocks"] for key in block)
