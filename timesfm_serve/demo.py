import csv
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

DATA = Path(__file__).resolve().parent.parent / "data" / "demand_daily.csv"
STATIC = Path(__file__).resolve().parent / "static"

# state capital, good enough for a state level covariate
COORDS = {
    "Karnataka": (12.97, 77.59),
    "Gujarat": (23.03, 72.58),
    "Delhi": (28.61, 77.21),
    "Maharashtra": (19.08, 72.88),
    "Tamil Nadu": (13.08, 80.27),
}

router = APIRouter()
_history = None


def history():
    global _history
    if _history is None:
        h = defaultdict(list)
        with DATA.open() as f:
            for row in csv.DictReader(f):
                h[row["state"]].append((row["date"], float(row["demand"])))
        _history = dict(h)
    return _history


@router.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@router.get("/demo/states")
def states():
    return [{"state": s, "lat": lat, "lon": lon} for s, (lat, lon) in COORDS.items()]


@router.get("/demo/history")
def state_history(state: str, days: int = 365):
    h = history().get(state)
    if h is None:
        raise HTTPException(404, "unknown state")
    rows = h[-days:]
    lat, lon = COORDS[state]
    return {"state": state, "lat": lat, "lon": lon, "dates": [r[0] for r in rows], "demand": [r[1] for r in rows]}
