from datetime import date, timedelta

import numpy as np

from timesfm_serve import weather


class FakeMeteo(weather.OpenMeteo):
    """serves canned responses so we can test the stitching without the network"""

    def __init__(self):
        super().__init__()
        self.calls = []

    def _get(self, url, params):
        self.calls.append((url, params["start_date"], params["end_date"]))
        days = (date.fromisoformat(params["end_date"]) - date.fromisoformat(params["start_date"])).days + 1
        base = 100.0 if "archive" in url else 200.0
        vals = [base + i for i in range(days)]
        if "archive" not in url:
            vals[-1] = None  # forecast api leaves the tail empty sometimes
        return {"daily": {"time": [""] * days, **{n: vals for n in weather.DAILY_VARS}}}


def test_archive_only_when_range_is_old():
    p = FakeMeteo()
    out = p.covariates(12.9, 77.6, date(2024, 1, 1), date(2024, 1, 10), "D")
    assert out.shape == (4, 10)
    assert len(p.calls) == 1 and "archive" in p.calls[0][0]


def test_recent_range_is_stitched_and_filled():
    p = FakeMeteo()
    end = date.today() + timedelta(days=14)
    start = end - timedelta(days=30)
    out = p.covariates(12.9, 77.6, start, end, "D")
    assert out.shape == (4, 31)
    assert len(p.calls) == 2
    assert not np.isnan(out).any()
    # forecast half starts where archive half ends
    assert out[0, 0] == 100.0 and out[0, -2] >= 200.0


def test_indus_is_a_stub():
    import pytest

    with pytest.raises(NotImplementedError):
        weather.Indus().covariates(0, 0, date(2024, 1, 1), date(2024, 1, 2))
