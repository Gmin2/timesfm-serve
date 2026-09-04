import pathlib

from redis import Redis
from rq import SimpleWorker

from timesfm_serve import db, jobs


def main():
    # SimpleWorker runs jobs in process instead of forking a work horse per job.
    # forking after torch has spun up its thread pool deadlocks on linux, and
    # we want the warmed model reused across jobs anyway.
    db.migrate()
    jobs.model()
    pathlib.Path("/tmp/worker-ready").touch()
    SimpleWorker([jobs.QUEUE], connection=Redis.from_url(jobs.REDIS_URL), log_job_description=False).work()


if __name__ == "__main__":
    main()
