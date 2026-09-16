"""The shape of everything the price API returns.

Declared as models rather than loose dicts so the OpenAPI document describes the
responses, and so a change to the payload has to be a deliberate change here.
"""

from datetime import date

from pydantic import AwareDatetime, BaseModel, Field, FiniteFloat

from iex.constants import BLOCKS, CAP


class PriceBlock(BaseModel):
    """One 15-minute settlement block. Block 1 is 00:00 to 00:15 IST."""

    block: int = Field(ge=1, le=BLOCKS, description="Settlement block, 1 to 96")
    forecast: FiniteFloat = Field(ge=0, le=CAP, description="Clearing price in rupees per MWh")
    q10: FiniteFloat | None = Field(default=None, ge=0, le=CAP)
    q20: FiniteFloat | None = Field(default=None, ge=0, le=CAP)
    q30: FiniteFloat | None = Field(default=None, ge=0, le=CAP)
    q40: FiniteFloat | None = Field(default=None, ge=0, le=CAP)
    q50: FiniteFloat | None = Field(default=None, ge=0, le=CAP)
    q60: FiniteFloat | None = Field(default=None, ge=0, le=CAP)
    q70: FiniteFloat | None = Field(default=None, ge=0, le=CAP)
    q80: FiniteFloat | None = Field(default=None, ge=0, le=CAP)
    q90: FiniteFloat | None = Field(default=None, ge=0, le=CAP)


class PriceForecast(BaseModel):
    """One delivery day, exactly as it was written at the cutoff."""

    delivery_date: date = Field(description="The day being bid for")
    model: str = Field(description="Which configuration produced it")
    issued_at: AwareDatetime = Field(description="When the forecast was written to the record")
    cutoff_at: AwareDatetime = Field(
        description="The information cutoff, 09:30 IST on the day before delivery")
    model_revision: str = Field(description="Pinned checkpoint the forecast came from")
    issued_live: bool = Field(
        description="True when written at its own cutoff, false when backfilled later")
    blocks: list[PriceBlock] = Field(min_length=BLOCKS, max_length=BLOCKS)
    notice: str


class ScoredDay(BaseModel):
    """How one issued forecast did, once the exchange settled the day."""

    delivery_date: date
    model: str
    blocks_scored: int = Field(ge=1, le=BLOCKS)
    mae: FiniteFloat = Field(ge=0, description="Mean absolute error, rupees per MWh")
    rmse: FiniteFloat = Field(ge=0)
    bias: FiniteFloat = Field(description="Mean signed error; positive means we forecast too high")
    coverage_p10_p90: FiniteFloat | None = Field(
        default=None, ge=0, le=1, description="Share of blocks the p10 to p90 band contained")
    issued_live: bool


class Summary(BaseModel):
    days: int = Field(ge=1)
    mae: FiniteFloat = Field(ge=0)
    rmse: FiniteFloat = Field(ge=0)
    coverage_p10_p90: FiniteFloat = Field(ge=0, le=1)


class Scorecard(BaseModel):
    """Every scored day, with live days kept apart from backfilled ones."""

    summary: Summary | None
    live_only: Summary | None = Field(
        description="The same averages over days written at their own cutoff only")
    days: list[ScoredDay]
    notice: str
    how_to_read: str


class ModelCard(BaseModel):
    """A configuration the API has forecasts on record for."""

    name: str
    delivery_days: int = Field(ge=0)
    first_delivery_date: date | None
    last_delivery_date: date | None
    revision: str
