"""Bounded CUDA-only output-head fine-tuning. Never modifies the serving checkpoint."""

import argparse
import copy
import hashlib
import inspect
import json
import os
import random
import time
from pathlib import Path

import numpy as np

from scripts.weather_model import MODEL_ID, MODEL_REVISION
from timesfm_serve.weather_learning import digest, metrics, validate_dataset
from timesfm_serve.weather_live_policy import STATIONS

RIDGE_MAX_STANDARDIZED_FEATURE = 6.0


def evaluate(examples, predictions):
    def score(selected):
        observed = [value for e in selected for value in e["targets"]]
        return {name: metrics(observed, [value for e in selected for value in
                                      (predictions[e["id"]] if name == "candidate" else e[name])])
                for name in ("candidate", "current", "ecmwf", "ridge")}

    report = {"overall": score(examples),
              "stations": {station: score([e for e in examples if e["station_id"] == station]) for station in STATIONS},
              "lead_bands": {}}
    for start, end in ((1, 6), (7, 24), (25, 48)):
        sliced = [{**e, **{name: e[name][start - 1:end] for name in ("targets", "current", "ecmwf", "ridge")}} for e in examples]
        estimates = {e["id"]: predictions[e["id"]][start - 1:end] for e in examples}
        observed = [v for e in sliced for v in e["targets"]]
        report["lead_bands"][f"{start}-{end}"] = {name: metrics(observed, [v for e in sliced for v in
            (estimates[e["id"]] if name == "candidate" else e[name])]) for name in ("candidate", "current", "ecmwf", "ridge")}
    return report


def review_gate(report, engineering_smoke=False, ridge=None):
    overall = report["overall"]
    enough = all(group["candidate"]["hours"] >= 120 for group in report["stations"].values())
    comparable = all(overall[name]["hours"] == overall["candidate"]["hours"] for name in ("current", "ecmwf", "ridge"))
    improved = overall["candidate"]["rmse_c"] is not None and all(
        overall[name]["rmse_c"] is not None and overall["candidate"]["rmse_c"] <= overall[name]["rmse_c"] * .98
        for name in ("current", "ecmwf", "ridge"))
    groups = [*report["stations"].values(), *report["lead_bands"].values()]
    stable = all(group["candidate"]["rmse_c"] is not None and group["current"]["rmse_c"] is not None
                 and group["candidate"]["rmse_c"] <= group["current"]["rmse_c"] * 1.05 for group in groups)
    # A residual correction scoring worse than the guidance it corrects is not a
    # comparator, it is a free bar. Refuse to grade a candidate against one.
    usable = (ridge is not None and ridge["in_distribution"]
              and overall["ridge"]["rmse_c"] is not None and overall["ecmwf"]["rmse_c"] is not None
              and overall["ridge"]["rmse_c"] <= overall["ecmwf"]["rmse_c"])
    return {"status": "review_required" if enough and comparable and improved and stable and usable and not engineering_smoke else "rejected",
            "checks": {"enough_observations": enough, "common_mask": comparable, "rmse_improvement_2_percent": improved,
                       "station_and_lead_regression_below_5_percent": stable, "ridge_comparator_valid": usable,
                       "not_engineering_smoke": not engineering_smoke},
            "automatic_promotion": False, "statistical_superiority_established": False,
            "license_scope": "non-commercial non-production research only"}


def ridge_baseline(examples, cutoff):
    """Fit the train-only Ridge comparator and report whether it can be trusted.

    A standardized linear model extrapolates badly when the validation origins sit
    far from the training origins in time. Seasonal features barely move across a
    short training span, so their scaler deviation is tiny and a validation row
    lands tens of deviations out. Measure that distance rather than trusting the
    resulting number; weather_correction.py is frozen and cannot be clamped.
    """
    import pandas as pd

    from scripts.weather_correction import FEATURES, apply_correction, fit_correction, prepare_case

    frames = []
    for example in examples:
        origin = pd.Timestamp(example["origin"])
        index = pd.date_range(origin - pd.Timedelta(hours=167), periods=216, freq="h")
        observed = pd.Series(example["observed_context"] + example["targets"], index=index, dtype=float)
        forecast = pd.Series(example["covariate"], index=index, dtype=float)
        frame, _ = prepare_case(observed, forecast, origin, example["station_id"])
        frame["split"] = "train" if example["split"] == "train" else "validation"
        frame["valid_time"] = frame.index
        frames.append(frame)
    trained = fit_correction(pd.concat([f for e, f in zip(examples, frames, strict=True) if e["split"] == "train"]), pd.Timestamp(cutoff))
    mean, scale = np.asarray(trained["mean"]), np.asarray(trained["scale"])
    distance = 0.0
    for example, frame in zip(examples, frames, strict=True):
        if example["split"] != "validation":
            continue
        example["ridge"] = apply_correction(frame, trained)["ridge_corrected_c"].tolist()
        standardized = (frame[FEATURES].to_numpy(dtype=float) - mean) / scale
        distance = max(distance, float(np.abs(standardized).max()))
    origins = sorted(pd.Timestamp(e["origin"]) for e in examples if e["split"] == "train")
    return {
        "training_windows": len(origins),
        "training_origin_span_days": (origins[-1] - origins[0]).total_seconds() / 86400,
        "max_standardized_feature": distance,
        "max_standardized_feature_limit": RIDGE_MAX_STANDARDIZED_FEATURE,
        "in_distribution": distance <= RIDGE_MAX_STANDARDIZED_FEATURE,
    }


def train(document, output, max_steps=64, max_seconds=600):
    validate_dataset(document)
    dataset_sha256 = digest(document)
    document = copy.deepcopy(document)
    if not 1 <= max_steps <= 256 or not 30 <= max_seconds <= 900:
        raise ValueError("Training exceeds the single-GPU time/step budget")
    if os.environ.get("WEATHER_TRAINING_CHILD") != "1":
        raise ValueError("Training must run under the exclusive GPU supervisor")
    os.environ.setdefault("TRITON_CACHE_DIR", "/tmp/triton-cache")
    os.environ.setdefault("TORCHINDUCTOR_CACHE_DIR", "/tmp/torchinductor-cache")
    os.environ.setdefault("CUDA_CACHE_PATH", "/tmp/cuda-cache")
    import torch
    import torch.backends.python_native as native_backends
    from safetensors.torch import load_file, save_file

    from scripts.weather_conditioned import TimesFMSession

    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise ValueError("Exactly one visible CUDA GPU is required; no CPU fallback")
    # Use precompiled ATen CUDA kernels: the hardened worker has no runtime C compiler.
    native_backends.triton.disable()
    started = time.monotonic()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Refusing to overwrite a training run")

    def deadline():
        if time.monotonic() - started >= max_seconds:
            raise TimeoutError("Training wall-time budget exhausted")

    examples = document["examples"]
    ridge = ridge_baseline(examples, document["validation_start"])
    session = TimesFMSession("cuda", cache_dir=os.environ.get("HF_HUB_CACHE"), local_files_only=True)
    model = session.model.model
    # Keep upstream covariate preparation, masking, and forecast extraction intact.
    # Only unwrap its no_grad decorator; never hand-roll a different training forecast.
    decode = getattr(type(model).decode, "__wrapped__", None)
    if decode is None or inspect.iscoroutinefunction(decode):
        raise ValueError("Pinned native decoding no longer exposes a gradient-capable function")
    model.requires_grad_(False)
    model.output_head.requires_grad_(True)
    parameters = list(model.output_head.parameters())
    trainable = sum(p.numel() for p in parameters)
    total = sum(p.numel() for p in model.parameters())
    if not 0 < trainable < total // 10:
        raise ValueError("Unexpected output-head size; refusing broad fine-tuning")
    original = {name: value.detach().cpu().clone() for name, value in model.output_head.state_dict().items()}
    torch.manual_seed(20260906)
    training = [e for e in examples if e["split"] == "train"]
    validation = [e for e in examples if e["split"] == "validation"]

    def tensors(example):
        context = torch.tensor(example["context"], dtype=torch.float32, device="cuda")[None, None, :]
        covariate = torch.tensor(example["covariate"], dtype=torch.float32, device="cuda")[None, None, :]
        return context, covariate

    def predict(example):
        deadline()
        output = session.model.predict(np.asarray(example["context"], dtype=np.float32), **session.options,
                                       past_future_covariates=np.asarray(example["covariate"], dtype=np.float32)[None, :])
        point, quantiles = np.asarray(output.forecast), np.asarray(output.quantiles)
        if point.shape != (48,) or quantiles.shape != (48, 9) or not np.isfinite(quantiles).all():
            raise ValueError("Non-finite or invalid candidate forecast")
        return point.tolist(), quantiles

    for example in validation:
        example["current"], _ = predict(example)
    probe = training[0]
    context, covariate = tensors(probe)
    with torch.no_grad():
        native = decode(model, context, horizon=48, past_future_covariates=covariate)[0, 0]
        _, expected = predict(probe)
        np.testing.assert_allclose(torch.sort(native, dim=-1).values.cpu().numpy(), expected, atol=1e-4, rtol=1e-5)
    del native, context, covariate
    optimizer = torch.optim.AdamW(parameters, lr=1e-5, weight_decay=.01)
    quantile_levels = torch.tensor(model.quantiles, dtype=torch.float32, device="cuda")
    order = list(training)
    random.Random(20260906).shuffle(order)
    losses = []
    for step in range(max_steps):
        deadline()
        example = order[step % len(order)]
        context, covariate = tensors(example)
        target = torch.tensor(np.asarray(example["targets"], dtype=np.float32), device="cuda")
        observed = torch.isfinite(target)
        optimizer.zero_grad(set_to_none=True)
        prediction = decode(model, context, horizon=48, past_future_covariates=covariate)[0, 0]
        error = target[observed, None] - prediction[observed]
        loss = torch.maximum(quantile_levels * error, (quantile_levels - 1) * error).mean()
        if not torch.isfinite(loss):
            raise ValueError("Non-finite training loss")
        loss.backward()
        norm = torch.nn.utils.clip_grad_norm_(parameters, 1.0, error_if_nonfinite=True)
        if float(norm) == 0:
            raise ValueError("No output-head gradient reached the checkpoint")
        optimizer.step()
        losses.append(float(loss.detach()))
        if step == 0 or (step + 1) % 16 == 0:
            print(json.dumps({"event": "weather_training_step", "step": step + 1, "loss": losses[-1]}), flush=True)
    predictions, coverage = {}, []
    for example in validation:
        predictions[example["id"]], quantiles = predict(example)
        target = np.asarray(example["targets"], dtype=float)
        mask = np.isfinite(target)
        coverage.extend(((target[mask] >= quantiles[mask, 0]) & (target[mask] <= quantiles[mask, -1])).tolist())
    checkpoint = output / "output-head.safetensors"
    tuned = {name: value.detach().cpu().contiguous() for name, value in model.output_head.state_dict().items()}
    if not any(not torch.equal(original[name], value) for name, value in tuned.items()):
        raise ValueError("Training did not change any model weights")
    save_file(tuned, str(checkpoint))
    if checkpoint.stat().st_size > 32 * 1024**2:
        raise ValueError("Checkpoint exceeds the bounded artifact budget")
    model.output_head.load_state_dict(original)
    model.output_head.load_state_dict(load_file(str(checkpoint)))
    reloaded, _ = predict(validation[0])
    np.testing.assert_allclose(reloaded, predictions[validation[0]["id"]], atol=1e-5, rtol=1e-6)
    scores = evaluate(validation, predictions)
    result = {
        "schema_version": 1, "base_model": MODEL_ID, "base_revision": MODEL_REVISION,
        "base_checkpoint_sha256": session.metadata["checkpoint_sha256"],
        "training": {"kind": "timesfm_output_head_finetune", "steps": max_steps, "trainable_parameters": trainable,
                     "fine_tuned": True, "updated_module": "output_head", "native_forecast_parity_verified": True,
                     "kernel_backend": "aten_cuda_no_runtime_triton_compilation",
                     "total_parameters": total, "first_loss": losses[0], "last_loss": losses[-1],
                     "seconds": time.monotonic() - started, "peak_gpu_bytes": torch.cuda.max_memory_allocated(),
                     "checkpoint_reload_verified": True, "learning_rate": 1e-5},
        "scores": scores, "candidate_p10_p90_coverage": float(np.mean(coverage)),
        "ridge_comparator": ridge,
        "packages": session.metadata["packages"],
        "decision": review_gate(scores, document.get("engineering_smoke", False), ridge),
        "dataset_sha256": dataset_sha256, "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "validation_scope": "rolling chronological validation, not an untouched final benchmark",
    }
    (output / "report.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"event": "weather_training_finished", "decision": result["decision"], "training": result["training"]}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-steps", type=int, default=64)
    parser.add_argument("--max-seconds", type=int, default=600)
    args = parser.parse_args()
    train(json.loads(args.dataset.read_text()), args.output, args.max_steps, args.max_seconds)


if __name__ == "__main__":
    main()
