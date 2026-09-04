from pathlib import Path

import numpy as np
import pandas as pd

RAW = Path(__file__).resolve().parent.parent / "tmp" / "claude" / "timesfm-serve" / "raw"

WEATHER_COLS = ["temp_mean", "temp_max", "ghi", "wind"]

# zenodo weather files use slightly different names than the demand columns
WX_FILE = {"Delhi": "NCT_of_Delhi"}


def load_state(state: str, start: str = "2019-01-01") -> pd.DataFrame:
    """daily peak demand (MW) for one state joined with era5 daily weather.

    returns a gap free daily frame with columns demand + WEATHER_COLS.
    small holes in demand are linearly interpolated.
    """
    demand = pd.read_csv(RAW / "zenodo_corrected_peak_met_MW.csv", parse_dates=["Date"]).set_index("Date")[state]
    wx = pd.read_csv(RAW / f"zenodo_{WX_FILE.get(state, state.replace(' ', '_'))}.csv", parse_dates=["date"]).set_index("date")
    demand = demand[~demand.index.duplicated(keep="last")]
    wx = wx[~wx.index.duplicated(keep="last")]

    df = pd.DataFrame({"demand": demand})
    df["temp_mean"] = wx["2m_temperature_mean"] - 273.15
    df["temp_max"] = wx["2m_temperature_max"] - 273.15
    df["ghi"] = wx["surface_solar_radiation_downwards_mean"] / 3600  # J/m2 -> Wh/m2 per hour, close enough
    df["wind"] = np.hypot(wx["10m_u_component_of_wind_mean"], wx["10m_v_component_of_wind_mean"])

    df = df.loc[start:]
    df = df.reindex(pd.date_range(df.index.min(), df.index.max(), freq="D"))
    df = df.interpolate(limit_direction="both")
    return df
