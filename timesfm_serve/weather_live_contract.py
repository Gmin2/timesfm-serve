from datetime import timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from timesfm_serve.weather_contract import ForecastPoint
from timesfm_serve.weather_live_policy import MAX_ISSUE_DELAY, MAX_OBSERVATION_LAG, POLICY, SERVE_MAX_AGE


class LiveForecastDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    input_snapshot_id: UUID
    case_id: str
    station_id: Literal["42410099999", "43128599999", "43279099999"]
    mode: Literal["experimental_live"]
    production_validated: Literal[False]
    policy: Literal["awc_metar_live_v1"]
    forecast_origin: AwareDatetime
    guidance_run: AwareDatetime
    inputs_captured_at: AwareDatetime
    latest_observation_at: AwareDatetime
    generated_at: AwareDatetime
    fresh_until: AwareDatetime
    horizon_hours: Literal[48]
    interval_hours: Literal[1]
    unit: Literal["celsius"]
    model: Literal["timesfm_nwp"]
    point_estimate: Literal["p50"]
    quantile_levels: list[float] = Field(min_length=9, max_length=9)
    quantiles_calibrated: Literal[False]
    model_provenance: dict
    input_provenance: dict
    points: list[ForecastPoint] = Field(min_length=48, max_length=48)

    @model_validator(mode="after")
    def validate_live_timing(self):
        origin = self.forecast_origin
        if origin.utcoffset() != timedelta(0) or origin.hour % 6 or origin.minute or origin.second or origin.microsecond:
            raise ValueError("Expected a six-hour UTC cycle")
        if self.case_id != f"{self.station_id}_{origin.strftime('%Y%m%dT%H%MZ')}" or self.policy != POLICY:
            raise ValueError("Live forecast identity mismatch")
        if self.guidance_run != origin - timedelta(hours=6):
            raise ValueError("Expected guidance initialized six hours before origin")
        if not origin + timedelta(minutes=15) <= self.inputs_captured_at <= self.generated_at <= origin + MAX_ISSUE_DELAY:
            raise ValueError("Inputs or generation time are outside the live issue window")
        if not timedelta(0) <= origin - self.latest_observation_at <= MAX_OBSERVATION_LAG:
            raise ValueError("Latest observation is stale or after the origin")
        if self.fresh_until != origin + SERVE_MAX_AGE or self.quantile_levels != [q / 10 for q in range(1, 10)]:
            raise ValueError("Incorrect freshness or quantile policy")
        if any(point.valid_time != origin + timedelta(hours=i) for i, point in enumerate(self.points, start=1)):
            raise ValueError("Expected 48 hourly targets from the context origin")
        return self


class LiveForecastResponse(LiveForecastDocument):
    published_at: AwareDatetime
    prospective_from: AwareDatetime

    @model_validator(mode="after")
    def publication_times(self):
        if not self.generated_at <= self.published_at <= self.forecast_origin + MAX_ISSUE_DELAY:
            raise ValueError("Forecast publication was not within the live issue window")
        expected = self.published_at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if self.prospective_from != expected:
            raise ValueError("Prospective targets must start strictly after publication")
        return self
