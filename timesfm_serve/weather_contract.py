from datetime import timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from timesfm_serve.weather_catalog import CASE_PATTERN


class ReplaySubmission(BaseModel):
    job_id: UUID
    created: bool
    mode: Literal["historical_replay"]


class WeatherJob(BaseModel):
    id: UUID
    case_id: str = Field(pattern=f"^{CASE_PATTERN}$")
    status: Literal["queued", "running", "succeeded", "failed"]
    attempts: int = Field(ge=0, le=3)
    credits_reserved: Literal[48]
    refunded: bool
    error_code: Literal["invalid_replay", "inference_failed", "worker_lease_expired"] | None
    created_at: AwareDatetime
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    mode: Literal["historical_replay"]
    forecast_url: str | None


class ForecastPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    valid_time: AwareDatetime
    temperature_2m: FiniteFloat
    ecmwf_ifs_temperature_2m: FiniteFloat
    quantiles: list[FiniteFloat] = Field(min_length=9, max_length=9)

    @model_validator(mode="after")
    def ordered_quantiles(self):
        if self.quantiles != sorted(self.quantiles) or abs(self.temperature_2m - self.quantiles[4]) > 1e-6:
            raise ValueError("Expected ordered p10-p90 quantiles with a p50 point estimate")
        return self


class ForecastDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    case_id: str = Field(pattern=f"^{CASE_PATTERN}$")
    station_id: Literal["42410099999", "43128599999", "43279099999"]
    mode: Literal["historical_replay"]
    operational_forecast: Literal[False]
    forecast_origin: AwareDatetime
    guidance_run: AwareDatetime
    generated_at: AwareDatetime
    horizon_hours: Literal[48]
    interval_hours: Literal[1]
    unit: Literal["celsius"]
    model: Literal["timesfm_nwp"]
    point_estimate: Literal["p50"]
    quantile_levels: list[FiniteFloat] = Field(min_length=9, max_length=9)
    quantiles_calibrated: Literal[False]
    model_provenance: dict
    input_provenance: dict
    points: list[ForecastPoint] = Field(min_length=48, max_length=48)

    @model_validator(mode="after")
    def aligned_times(self):
        origin = self.forecast_origin
        if origin.utcoffset() != timedelta(0) or (origin.hour, origin.minute, origin.second, origin.microsecond) != (6, 0, 0, 0):
            raise ValueError("Expected a 06:00 UTC forecast origin")
        if self.case_id != f"{self.station_id}_{origin.strftime('%Y%m%dT%H%MZ')}":
            raise ValueError("Case identity does not match the forecast")
        if self.guidance_run != origin - timedelta(hours=6) or self.generated_at <= origin + timedelta(hours=48):
            raise ValueError("Replay timestamps are inconsistent")
        if self.quantile_levels != [q / 10 for q in range(1, 10)]:
            raise ValueError("Expected p10-p90 quantile levels")
        if any(point.valid_time != origin + timedelta(hours=i) for i, point in enumerate(self.points, start=1)):
            raise ValueError("Expected 48 consecutive hourly forecast targets")
        return self
