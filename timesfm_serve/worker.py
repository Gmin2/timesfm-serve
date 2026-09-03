from redis import Redis
from rq import Worker

from timesfm_serve import jobs


def main():
    jobs.model()  # warm the model once per worker process
    Worker([jobs.QUEUE], connection=Redis.from_url(jobs.REDIS_URL)).work()


if __name__ == "__main__":
    main()
