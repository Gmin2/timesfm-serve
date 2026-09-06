"""Read live scoring and training status; never starts training or changes compute."""

import json

from psycopg.rows import dict_row

from timesfm_serve import db


def main():
    db.initialize()
    try:
        with db.conn() as connection, connection.cursor(row_factory=dict_row) as cursor:
            scores = cursor.execute(
                "select count(*) as forecasts, max(checked_at) as last_checked,"
                " coalesce(sum((report->'current'->>'hours')::int),0) as matched_forecast_hours from weather_evaluations"
            ).fetchone()
            runs = cursor.execute(
                "select id, created_at, finished_at, status, artifact_prefix, report from weather_training_runs"
                " order by created_at desc limit 5"
            ).fetchall()
        print(json.dumps({"scores": scores, "training_runs": runs, "automatic_promotion": False}, default=str, indent=2))
    finally:
        db.pool.close()


if __name__ == "__main__":
    main()
