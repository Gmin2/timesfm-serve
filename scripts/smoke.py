import time
import numpy as np
import timesfm

t0 = time.time()
model = timesfm.TimesFM3Forecaster.from_pretrained("google/timesfm-3.0-pytorch", device="cpu")
print(f"loaded in {time.time() - t0:.1f}s")

# fake hourly demand: daily cycle + weekly cycle + trend + noise
hours = np.arange(24 * 30)
demand = (
    1000
    + 200 * np.sin(2 * np.pi * hours / 24)
    + 80 * np.sin(2 * np.pi * hours / (24 * 7))
    + 0.5 * hours
    + np.random.default_rng(0).normal(0, 20, hours.size)
)

t0 = time.time()
out = model.predict(demand, horizon=48, return_quantiles=True)
print(f"forecast in {time.time() - t0:.1f}s")
print("forecast", out.forecast.shape, out.forecast[:6].round(1))
print("quantiles", out.quantiles.shape)
print("last 6 of context", demand[-6:].round(1))
print("q10/q50/q90 at h=1", out.quantiles[0, 0].round(1), out.quantiles[0, 4].round(1), out.quantiles[0, 8].round(1))
