import json
from datetime import date

import pandas as pd
import pytest

from scripts.iex_data import audit, block_of, cap_for, months, normalise


def snapshot(market, rows):
    return json.dumps({"statusCode": 200, "message": "ok", "data": rows}).encode()


def test_block_numbering_handles_both_period_formats():
    # RTM dropped the spaces on 22-03-2022 and mixes both formats on the changeover day.
    assert block_of("00:00 - 00:15") == block_of("00:00-00:15") == 1
    assert block_of("13:00 - 13:15") == block_of("13:00-13:15") == 53
    assert block_of("23:45 - 24:00") == 96
    for period in ("24:00 - 24:15", "00:07 - 00:22", "nonsense"):
        with pytest.raises(ValueError):
            block_of(period)


def test_price_key_and_types_differ_by_market():
    dam = normalise("dam", snapshot("dam", [{"date": "12-11-2024", "period": "00:00 - 00:15", "mcp": "3060.76", "congestion": "NO", "mcv": "7270.19"}]))
    # GDAM returns numbers, not strings, under a different key.
    gdam = normalise("gdam", snapshot("gdam", [{"date": "12-11-2024", "period": "00:00 - 00:15", "unconstrained_m_c_p": 5000.67, "congestion": "NO"}]))
    assert dam[0]["price"] == 3060.76 and dam[0]["block"] == 1
    assert gdam[0]["price"] == 5000.67
    # HP-DAM prices are null when nothing clears, and ISO dates appear on some routes.
    hpdam = normalise("hpdam", snapshot("hpdam", [{"date": "2024-11-12", "period": "00:00 - 00:15", "mcp": None, "congestion": "NO"}]))
    assert hpdam[0]["price"] is None and hpdam[0]["delivery_date"] == date(2024, 11, 12)


def test_cap_regimes_follow_the_cerc_orders():
    assert cap_for(date(2022, 4, 2), "dam") == 20_000
    assert cap_for(date(2022, 4, 3), "dam") == 12_000
    assert cap_for(date(2023, 4, 3), "dam") == 12_000
    assert cap_for(date(2023, 4, 4), "dam") == 10_000
    assert cap_for(date(2026, 9, 15), "dam") == 10_000
    assert cap_for(date(2026, 9, 15), "hpdam") == 20_000


def test_audit_separates_suspended_blocks_from_real_zero_prices():
    rows = [
        {"market": "rtm", "delivery_date": date(2026, 5, 1), "block": b, "price": 100.0, "mcv": 50.0, "congestion": False}
        for b in range(1, 97)
    ]
    rows[0] |= {"price": 0.0, "mcv": 0.0}   # suspended session
    rows[1] |= {"price": 0.0, "mcv": 900.0}  # real clearing at the floor
    frame = audit(pd.DataFrame(rows))
    assert bool(frame.loc[frame["block"] == 1, "suspended"].item()) is True
    assert bool(frame.loc[frame["block"] == 2, "suspended"].item()) is False
    assert bool(frame.loc[frame["block"] == 1, "usable"].item()) is False
    assert bool(frame.loc[frame["block"] == 2, "usable"].item()) is True


def test_audit_marks_short_days_unusable_and_flags_the_cap():
    rows = [
        {"market": "dam", "delivery_date": date(2024, 7, 17), "block": b, "price": 10_000.0, "mcv": 5.0, "congestion": False}
        for b in range(1, 95)
    ]
    frame = audit(pd.DataFrame(rows))
    assert not frame["complete_day"].any() and not frame["usable"].any()
    assert frame["at_cap"].all()


def test_months_covers_every_day_once():
    spans = list(months(date(2022, 1, 15), date(2022, 4, 3)))
    assert spans[0] == (date(2022, 1, 15), date(2022, 1, 31))
    assert spans[-1] == (date(2022, 4, 1), date(2022, 4, 3))
    assert sum((last - first).days + 1 for first, last in spans) == (date(2022, 4, 3) - date(2022, 1, 15)).days + 1
