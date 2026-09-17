"""The gateway both applications are served behind.

One FastAPI app, one database, one cluster. Weather and power prices are
separate problems with separate code that never import each other; they meet
here and nowhere else, which is the only place sharing actually pays.

The weather app is taken as it is and extended, so its routes, its contract and
its frozen experiment code are untouched by any of this.
"""

from iex.api import router as iex_router
from timesfm_serve.weather_api import app

app.title = "TimesFM Forecast API"
app.description = ("Day-ahead forecasts from TimesFM 3.0, used zero-shot. "
                   "/v1/weather covers station temperature, /v1/iex covers "
                   "Indian power exchange day-ahead prices.")
app.include_router(iex_router)
