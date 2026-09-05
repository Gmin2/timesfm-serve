# Weather Data

The active project forecasts station temperature, not electricity demand.

- `data/weather/raw/` holds ignored provider-download caches.
- `results/weather/` contains versioned experiment manifests, input snapshots,
  predictions and scores needed to reproduce the weather comparisons.
- Live ingestion stores captured inputs and forecast records in PostgreSQL.
- Model weights and local tooling belong in ignored caches, not this directory.

See [the historical data parser](../scripts/weather_seasonal_data.py),
[the multi-season protocol](../experiments/weather_seasonal_v1.json) and
[the live source policy](../timesfm_serve/weather_live_policy.py).

The former `data/demand_daily.csv` and its provenance remain in Git at commit
`14002b05619c7894e3b65bb19c1970a3d06bd32a`.
