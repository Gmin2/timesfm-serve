"""Allowlisted, immutable historical inputs, never a live observation feed."""

import hashlib
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPLAY_ROOT = Path(os.environ.get(
    "WEATHER_REPLAY_ROOT", ROOT / "results/weather/india_station_seasonal_v1/holdout/cases"
)).resolve()
CASE_PATTERN = r"(?:42410099999|43128599999|43279099999)_\d{8}T0600Z"


def case_manifest(case_id: str):
    if re.fullmatch(CASE_PATTERN, case_id) is None:
        raise ValueError("invalid_replay_case")
    directory = (REPLAY_ROOT / case_id).resolve()
    if directory.parent != REPLAY_ROOT:
        raise ValueError("invalid_replay_case")
    raw = (directory / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    if manifest.get("status") != "scored":
        raise ValueError("replay_case_unavailable")
    return directory, manifest, hashlib.sha256(raw).hexdigest()


def catalog():
    entries = []
    for path in sorted(REPLAY_ROOT.glob("*/manifest.json")):
        try:
            _, manifest, digest = case_manifest(path.parent.name)
        except (OSError, ValueError):
            continue
        entries.append({
            "case_id": path.parent.name, "station_id": manifest["station"],
            "forecast_origin": manifest["origin"], "guidance_run": manifest["run"],
            "manifest_sha256": digest,
        })
    return entries
