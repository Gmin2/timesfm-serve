import os

import numpy as np
from redis import Redis
from rq import Queue

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
QUEUE = "forecast"

_model = None


def queue():
    return Queue(QUEUE, connection=Redis.from_url(REDIS_URL))


def model():
    global _model
    if _model is None:
        import timesfm

        _model = timesfm.TimesFM3Forecaster.from_pretrained(os.environ.get("MODEL_ID", "google/timesfm-3.0-pytorch"), device=os.environ.get("DEVICE", "cpu"))
    return _model


def run_batch(job_id: str, items: list[dict], horizon: int):
    """rq task. items: [{id, series, past_covariates?, future_covariates?}]"""
    from timesfm_serve import db

    db.set_job_status(job_id, "running")
    try:
        ctxs = [np.asarray(it["series"], dtype=np.float32) for it in items]
        past = [np.asarray(it["past_covariates"], dtype=np.float32) if it.get("past_covariates") else None for it in items]
        fut = [np.asarray(it["future_covariates"], dtype=np.float32) if it.get("future_covariates") else None for it in items]
        outs = model().predict_batch(
            ctxs,
            horizon=horizon,
            past_only_covariates=past if any(p is not None for p in past) else None,
            past_future_covariates=fut if any(f is not None for f in fut) else None,
            return_quantiles=True,
        )
        rows = [(it["id"], o.forecast.tolist(), o.quantiles.tolist()) for it, o in zip(items, outs, strict=True)]
        db.save_results(job_id, rows)
        db.set_job_status(job_id, "done")
    except Exception as e:
        db.set_job_status(job_id, "failed", error=str(e)[:2000])
        # the batch was charged when it was queued, so give it back
        found = db.job_account_and_points(job_id)
        if found:
            db.refund_credits(found[0], found[1])
        raise


def reconcile(job_id: str, status: str) -> str:
    """db says queued/running. ask rq what it thinks, and mark lost jobs failed."""
    from rq import Worker
    from rq.job import Job, NoSuchJobError

    from timesfm_serve import db

    if status not in ("queued", "running"):
        return status
    r = Redis.from_url(REDIS_URL)
    try:
        job = Job.fetch(job_id, connection=r)
    except NoSuchJobError:
        db.set_job_status(job_id, "failed", error="job missing from queue")
        return "failed"
    rq_status = job.get_status(refresh=True)
    if rq_status in ("failed", "stopped", "canceled"):
        db.set_job_status(job_id, "failed", error=f"rq status {rq_status}")
        return "failed"
    if rq_status == "started":
        live = {w.name for w in Worker.all(connection=r)}
        if job.worker_name not in live:
            db.set_job_status(job_id, "failed", error="worker died mid job")
            return "failed"
    return status
