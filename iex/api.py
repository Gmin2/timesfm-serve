"""Read-only API for the day-ahead price forecasts we have already issued.

Forecasts are published here after the fact, never computed on request: the
whole point is that the record was written before the answer was known. A
request that arrives at noon cannot make the morning's forecast any better.

Raw exchange prices are never served. The IEX terms are personal and
non-commercial, so this publishes our own forecasts and our own error only.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from iex import store
from timesfm_serve import db
from timesfm_serve.auth import require_key

router = APIRouter(prefix="/v1/iex", tags=["IEX day-ahead prices"])
ATTRIBUTION = ("Forecasts of Indian Energy Exchange day-ahead prices. "
               "Underlying market data (c) IEX, used for personal, non-commercial "
               "research; no exchange prices are redistributed here.")


def _present(record):
    return {
        "delivery_date": record["delivery_date"].isoformat(),
        "model": record["model"],
        "issued_at": record["issued_at"].isoformat(),
        "cutoff_at": record["cutoff_at"].isoformat(),
        "model_revision": record["revision"],
        # true only when this was written at its own cutoff rather than backfilled
        "issued_live": record["issued_live"],
        "blocks": record["blocks"],
        "notice": ATTRIBUTION,
    }


@router.get("/forecast/latest", dependencies=[Depends(require_key)])
def latest_forecast(model: str | None = None):
    """The most recent delivery day we have a forecast on record for."""
    with db.conn() as connection:
        record = store.latest(connection, model)
    if record is None:
        raise HTTPException(status_code=404, detail="no forecast has been issued yet")
    return _present(record)


@router.get("/forecast/{delivery_date}", dependencies=[Depends(require_key)])
def forecast_for(delivery_date: date, model: str | None = None):
    """One delivery day, as it was forecast at the cutoff."""
    with db.conn() as connection:
        record = store.for_day(connection, delivery_date, model)
    if record is None:
        raise HTTPException(status_code=404,
                            detail=f"no forecast on record for {delivery_date.isoformat()}")
    return _present(record)


@router.get("/scorecard", dependencies=[Depends(require_key)])
def scorecard(days: int = Query(30, ge=1, le=365)):
    """How the issued forecasts actually did, once each day settled."""
    with db.conn() as connection:
        card = store.scorecard(connection, days)
    return {
        "summary": card["summary"],
        "live_only": card["live_only"],
        "days": [dict(row, delivery_date=row["delivery_date"].isoformat()) for row in card["days"]],
        "notice": ATTRIBUTION,
        "how_to_read": ("The summary covers every scored day. The live figures cover only days "
                        "whose forecast was written at its own 09:30 cutoff rather than filled in "
                        "later. Backfilled days were built under the same information cutoff and "
                        "leak nothing, but they were never standing predictions, so they are "
                        "reported apart."),
    }
