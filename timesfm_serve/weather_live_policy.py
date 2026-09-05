"""Explicit limits for the experimental live pilot, separate from the backtest."""

from datetime import datetime, timedelta, timezone

STATIONS = {
    "42410099999": {"id": "42410099999", "name": "Guwahati", "icao": "VEGT", "latitude": 26.1, "longitude": 91.58, "elevation_m": 54.0},
    "43128599999": {"id": "43128599999", "name": "Hyderabad", "icao": "VOHS", "latitude": 17.2333, "longitude": 78.4167, "elevation_m": 617.0},
    "43279099999": {"id": "43279099999", "name": "Chennai", "icao": "VOMM", "latitude": 12.9944, "longitude": 80.1805, "elevation_m": 15.8},
}
POLICY = "awc_metar_live_v1"
MAX_ISSUE_DELAY = timedelta(hours=3)
SERVE_MAX_AGE = timedelta(hours=8)
MAX_OBSERVATION_LAG = timedelta(hours=1)
SOURCE_CODE = ("weather_model.py", "weather_conditioned.py", "weather_correction.py", "weather_case.py")


class LiveInputError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def database_now(connection):
    return connection.execute("select clock_timestamp()").fetchone()[0]


def parse_utc(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise LiveInputError("timezone_required")
    return result.astimezone(timezone.utc)


def current_origin(now):
    candidate = parse_utc(now) - timedelta(minutes=15)
    return candidate.replace(hour=candidate.hour // 6 * 6, minute=0, second=0, microsecond=0)


def check_issue_window(origin, now):
    origin, now = parse_utc(origin), parse_utc(now)
    if origin.hour % 6 or origin.minute or origin.second or origin.microsecond:
        raise LiveInputError("invalid_live_origin")
    if not timedelta(minutes=15) <= now - origin <= MAX_ISSUE_DELAY:
        raise LiveInputError("outside_live_issue_window")


def input_digest(document):
    import hashlib
    import json

    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
