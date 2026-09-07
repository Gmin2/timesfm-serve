"""Score published live forecasts against later observations. No GPU, no model."""

import json
import logging

from timesfm_serve import db
from timesfm_serve.weather_learning import load_live_examples, refresh_scores
from timesfm_serve.weather_live_policy import utc_now

logger = logging.getLogger("weather-score")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db.initialize()
    try:
        now = utc_now()
        summary = refresh_scores(load_live_examples(now), now)
        logger.info(json.dumps({"event": "weather_live_scored", "as_of": now.isoformat(), **summary}))
    finally:
        db.pool.close()


if __name__ == "__main__":
    main()
