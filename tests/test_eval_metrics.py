import numpy as np

from scripts.eval import crps_from_quantiles, mape


def test_mape_zero_when_perfect():
    y = np.array([[10.0, 20.0]])
    assert mape(y, y).tolist() == [0.0]


def test_crps_smaller_for_tighter_correct_quantiles():
    y = np.full(5, 100.0)
    tight = np.tile(np.linspace(99, 101, 9), (5, 1))
    wide = np.tile(np.linspace(50, 150, 9), (5, 1))
    assert crps_from_quantiles(y, tight) < crps_from_quantiles(y, wide)
