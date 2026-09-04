import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "client"))
from timesfm_serve_client import Client

from timesfm_serve.data import WEATHER_COLS, load_state

c = Client(os.environ.get("TFM_URL", "http://localhost:8000"), os.environ["TFM_KEY"])

df = load_state("Karnataka")
hist = df.iloc[-379:-14]
fut = df.iloc[-14:]

r = c.forecast(hist["demand"], horizon=14, future_covariates=[hist[k].tolist() + fut[k].tolist() for k in WEATHER_COLS])
print("sync p50 first 3:", [round(x) for x in r["forecast"][:3]], "actual:", fut["demand"].iloc[:3].round().tolist())

job = c.submit([{"id": s, "series": load_state(s)["demand"].iloc[-379:-14].tolist()} for s in ["Karnataka", "Gujarat", "Delhi"]])
res = c.wait(job)
print("batch", res["status"], [(x["id"], round(x["forecast"][0])) for x in res["results"]])
