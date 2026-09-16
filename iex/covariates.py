"""Covariates for TimesFM, built so that nothing after the cutoff can enter.

TimesFM 3.0 takes two kinds. A past-and-future covariate must have a value for
every block of the delivery day, so it can only be something knowable in advance:
the calendar qualifies, tomorrow's prices do not. A past-only covariate stops at
the cutoff, which is where yesterday's bid volumes belong.
"""

import functools

import numpy as np
import pandas as pd

from iex.backtest import BLOCKS

DAY = pd.Timedelta(days=1)


@functools.cache
def holiday_dates(first_year, last_year):
    import holidays

    return frozenset(holidays.India(years=range(first_year, last_year + 1)))


def is_holiday(day):
    return pd.Timestamp(day).date() in holiday_dates(2021, 2027)


def calendar(days):
    """Four future covariates over a run of delivery days, each shaped (days * 96,).

    Block of day is split into a sine and cosine so midnight sits next to 23:45
    rather than at the opposite end of a ramp.
    """
    blocks = np.arange(BLOCKS)
    angle = 2 * np.pi * blocks / BLOCKS
    rows = {"block_sin": [], "block_cos": [], "weekend": [], "holiday": []}
    for day in days:
        stamp = pd.Timestamp(day)
        rows["block_sin"].append(np.sin(angle))
        rows["block_cos"].append(np.cos(angle))
        rows["weekend"].append(np.full(BLOCKS, float(stamp.dayofweek >= 5)))
        rows["holiday"].append(np.full(BLOCKS, float(is_holiday(stamp))))
    return {name: np.concatenate(values) for name, values in rows.items()}


def bid_ratio(history, market="dam"):
    """Sell bids over purchase bids, per block, for every settled day.

    Above one means more was offered than sought, which is the clearest
    supply-versus-demand signal the exchange publishes. Only days already
    settled at the cutoff are visible, so this is past-only.
    """
    sell = history.column("sell_bid", market)
    purchase = history.column("purchase_bid", market)
    ratio = sell / purchase.replace(0, np.nan)
    return ratio.reindex(columns=range(1, BLOCKS + 1))


def future_calendar(history, context_days):
    """Calendar values spanning the context plus the delivery day being forecast."""
    index = history.prices().index[-context_days:]
    span = list(index) + [history.delivery_date]
    return calendar(span)


def past_bid_ratio(history, context_days, market="dam"):
    """Bid ratio over the context only, gaps filled forward then flattened."""
    ratio = bid_ratio(history, market).tail(context_days)
    filled = ratio.ffill().bfill().fillna(1.0)
    return filled.to_numpy().reshape(-1)
