"""Constants the serving path needs, with no scientific stack behind them.

The API image is deliberately lightweight: it reads stored forecasts and never
runs a model. Keeping these here means `iex.api` and `iex.store` can be imported
without pulling in numpy or pandas.
"""

BLOCKS = 96
QUANTILE_LEVELS = tuple(level / 10 for level in range(1, 10))
COLUMNS = [f"q{int(level * 100)}" for level in QUANTILE_LEVELS]
# CERC caps the day-ahead price; nothing above this can clear.
CAP = 10_000.0
