"""weather covariate providers.

a provider returns hourly or daily weather for a point, covering both the
history window (for the context) and the horizon (for the past future
covariates). open meteo is the free stand in. indus is the slot for pravah's
model and is intentionally unimplemented.
"""

from datetime import date, timedelta
from typing import Protocol

import numpy as np
import requests

DAILY_VARS = ["temperature_2m_mean", "temperature_2m_max", "shortwave_radiation_sum", "wind_speed_10m_max"]
HOURLY_VARS = ["temperature_2m", "shortwave_radiation", "wind_speed_100m"]


class WeatherProvider(Protocol):
    name: str

    def covariates(self, lat: float, lon: float, start: date, end: date, freq: str) -> np.ndarray:
        """returns array shaped (n_features, n_steps) for start..end inclusive.

        steps are daily for freq 'D' and hourly for freq 'H'. rows are in the
        order of feature_names(). the caller decides how many of those steps
        are context and how many are horizon.
        """

    def feature_names(self, freq: str) -> list[str]: ...


class OpenMeteo:
    """archive api for the past, forecast api for the next 16 days, stitched."""

    name = "open-meteo"
    archive = "https://archive-api.open-meteo.com/v1/archive"
    forecast = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, timeout=30):
        self.timeout = timeout

    def feature_names(self, freq):
        return DAILY_VARS if freq == "D" else HOURLY_VARS

    def _get(self, url, params):
        r = requests.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def covariates(self, lat, lon, start, end, freq="D"):
        key = "daily" if freq == "D" else "hourly"
        names = self.feature_names(freq)
        today = date.today()
        # archive lags a few days, forecast api has ~90 days of past and 16 ahead
        split = min(end, today - timedelta(days=7))

        frames = []
        if start <= split:
            d = self._get(self.archive, dict(latitude=lat, longitude=lon, start_date=start.isoformat(), end_date=split.isoformat(), timezone="Asia/Kolkata", **{key: ",".join(names)}))
            frames.append((d[key]["time"], [d[key][n] for n in names]))
        if end > split:
            d = self._get(self.forecast, dict(latitude=lat, longitude=lon, start_date=(split + timedelta(days=1)).isoformat(), end_date=end.isoformat(), timezone="Asia/Kolkata", **{key: ",".join(names)}))
            frames.append((d[key]["time"], [d[key][n] for n in names]))

        cols = [np.asarray(f[1], dtype=np.float32) for f in frames]
        out = np.concatenate(cols, axis=1) if len(cols) > 1 else cols[0]
        # open meteo returns nulls for hours it does not have yet, fill forward
        for row in out:
            mask = np.isnan(row)
            if mask.any():
                idx = np.where(~mask, np.arange(len(row)), 0)
                np.maximum.accumulate(idx, out=idx)
                row[mask] = row[idx][mask]
        return out


class Indus:
    """pravah's weather model. same contract, not public yet."""

    name = "indus"

    def feature_names(self, freq):
        return ["ghi", "wind_100m", "temperature_2m"]

    def covariates(self, lat, lon, start, end, freq="D"):
        raise NotImplementedError("indus api is not public. plug the client in here and return (3, n_steps).")


PROVIDERS = {"open-meteo": OpenMeteo, "indus": Indus}


def get_provider(name: str) -> WeatherProvider:
    return PROVIDERS[name]()


def provider_names() -> list[str]:
    return list(PROVIDERS)
