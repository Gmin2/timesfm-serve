"""tiny client for timesfm-serve. no deps beyond requests."""

import time

import requests


class Client:
    def __init__(self, base_url, api_key, timeout=60):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["x-api-key"] = api_key
        self.timeout = timeout

    def _post(self, path, body):
        r = self.s.post(self.base + path, json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def forecast(self, series, horizon=14, past_covariates=None, future_covariates=None):
        return self._post("/forecast", dict(series=list(series), horizon=horizon, past_covariates=past_covariates, future_covariates=future_covariates))

    def forecast_with_weather(self, series, last_date, lat, lon, horizon=14, provider=None):
        body = dict(series=list(series), last_date=str(last_date), lat=lat, lon=lon, horizon=horizon)
        if provider:
            body["provider"] = provider
        return self._post("/forecast/weather", body)

    def submit(self, items, horizon=14):
        """items: [{id, series, past_covariates?, future_covariates?}]"""
        return self._post("/jobs", dict(items=items, horizon=horizon))["job_id"]

    def job(self, job_id):
        r = self.s.get(f"{self.base}/jobs/{job_id}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def wait(self, job_id, poll=1.0, timeout=600):
        t0 = time.time()
        while True:
            j = self.job(job_id)
            if j["status"] in ("done", "failed"):
                return j
            if time.time() - t0 > timeout:
                raise TimeoutError(f"job {job_id} still {j['status']} after {timeout}s")
            time.sleep(poll)
