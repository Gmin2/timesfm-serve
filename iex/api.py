"""Read-only API for the day-ahead price forecasts we have already issued.

Forecasts are published here after the fact, never computed on request: the whole
point is that the record was written before the answer was known. A request that
arrives at noon cannot make the morning's forecast any better.

Raw exchange prices are never served. The IEX terms are personal and
non-commercial, so this publishes our own forecasts and our own error only.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from iex import store
from iex.contract import ModelCard, PriceForecast, Scorecard
from timesfm_serve import db
from timesfm_serve.auth import require_key

router = APIRouter(prefix="/v1/iex", tags=["IEX day-ahead prices"])
ATTRIBUTION = ("Forecasts of Indian Energy Exchange day-ahead prices. "
               "Underlying market data (c) IEX, used for personal, non-commercial "
               "research; no exchange prices are redistributed here.")
HOW_TO_READ = ("The summary covers every scored day. The live figures cover only days "
               "whose forecast was written at its own 09:30 cutoff rather than filled in "
               "later. Backfilled days were built under the same information cutoff and "
               "leak nothing, but they were never standing predictions, so they are "
               "reported apart.")
MISSING = {404: {"description": "No forecast is on record for that delivery day"}}


def _present(record):
    return {
        "delivery_date": record["delivery_date"],
        "model": record["model"],
        "issued_at": record["issued_at"],
        "cutoff_at": record["cutoff_at"],
        "model_revision": record["revision"],
        "issued_live": record["issued_live"],
        "blocks": record["blocks"],
        "notice": ATTRIBUTION,
    }


@router.get("/forecast/latest", response_model=PriceForecast, responses=MISSING,
            summary="Latest issued forecast",
            dependencies=[Depends(require_key)])
def latest_forecast(model: Annotated[str | None, Query(description="Restrict to one model")] = None):
    """The most recent delivery day we have a forecast on record for.

    Issued at 09:30 IST the day before delivery, half an hour before bidding opens.
    """
    with db.conn() as connection:
        record = store.latest(connection, model)
    if record is None:
        raise HTTPException(status_code=404, detail="no forecast has been issued yet")
    return _present(record)


@router.get("/forecast/{delivery_date}", response_model=PriceForecast, responses=MISSING,
            summary="Forecast for one delivery day",
            dependencies=[Depends(require_key)])
def forecast_for(
    delivery_date: Annotated[date, Path(description="Delivery day, as YYYY-MM-DD")],
    model: Annotated[str | None, Query(description="Restrict to one model")] = None,
):
    """One delivery day, exactly as it was forecast at the cutoff.

    Forecasts are written once and never updated, so this is the prediction as it
    stood before the market cleared.
    """
    with db.conn() as connection:
        record = store.for_day(connection, delivery_date, model)
    if record is None:
        raise HTTPException(status_code=404,
                            detail=f"no forecast on record for {delivery_date.isoformat()}")
    return _present(record)


@router.get("/scorecard", response_model=Scorecard, summary="Track record",
            dependencies=[Depends(require_key)])
def scorecard(days: Annotated[int, Query(ge=1, le=365, description="How many settled days to return")] = 30):
    """How the issued forecasts actually did, once each day settled.

    A day is scored only after the exchange has published all 96 of its blocks.
    """
    with db.conn() as connection:
        card = store.scorecard(connection, days)
    return {"summary": card["summary"], "live_only": card["live_only"], "days": card["days"],
            "notice": ATTRIBUTION, "how_to_read": HOW_TO_READ}


@router.get("/models", response_model=list[ModelCard], summary="Models on record",
            dependencies=[Depends(require_key)])
def models():
    """Which model configurations have forecasts stored, and over what span."""
    with db.conn() as connection:
        return store.models(connection)
