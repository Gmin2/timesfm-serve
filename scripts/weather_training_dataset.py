"""Export a small, explicitly non-promotable trainer smoke dataset from saved development cases."""

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.weather_conditioned import validate_inputs
from scripts.weather_model import prepare_history
from timesfm_serve.weather_learning import validate_dataset
from timesfm_serve.weather_live_policy import STATIONS, utc_now


def export(root):
    examples = []
    for station in STATIONS:
        cases = sorted(root.glob(f"{station}_*/manifest.json"))
        if len(cases) < 3:
            raise ValueError("At least three development cases per station are required")
        for path in (cases[0], cases[1], cases[-1]):
            manifest = json.loads(path.read_text())
            frames = {}
            for name in ("model_input.csv", "guidance.csv", "predictions.csv"):
                raw = path.with_name(name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != manifest["artifact_sha256"][name]:
                    raise ValueError("Development artifact checksum mismatch")
                frames[name] = pd.read_csv(path.with_name(name), index_col="valid_time", parse_dates=["valid_time"])
            origin = pd.Timestamp(manifest["origin"])
            history = prepare_history(frames["model_input.csv"]["observed_c"], origin, context_hours=168)
            context, covariate = validate_inputs(history, frames["guidance.csv"])
            points = frames["predictions.csv"]
            examples.append({
                "id": path.parent.name, "station_id": station, "origin": origin.isoformat(),
                "split": "validation" if path == cases[-1] else "train",
                "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "context": context.tolist(), "covariate": covariate.tolist(),
                "observed_context": [None if pd.isna(v) else float(v) for v in history["observed_c"]],
                "targets": [None if pd.isna(v) else float(v) for v in points["observed_c"]],
                "current": points["timesfm_nwp_c"].tolist(), "ecmwf": covariate[-48:].tolist(),
            })
    cutoff = min(e["origin"] for e in examples if e["split"] == "validation")
    result = {"schema_version": 1, "purpose": "research_only", "engineering_smoke": True,
              "as_of": utc_now().isoformat(), "validation_start": cutoff,
              "observation_policy": "saved_GHCNh_development_not_fresh_holdout", "examples": examples}
    validate_dataset(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = export(args.development_cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, allow_nan=False) + "\n")
    print(json.dumps({"examples": len(result["examples"]), "engineering_smoke": True, "gpu_used": False}))


if __name__ == "__main__":
    main()
