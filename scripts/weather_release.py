"""Build local, deterministic artifact bundles. This command never uploads to AWS."""

import argparse
import gzip
import hashlib
import json
import tarfile
from pathlib import Path

from scripts.weather_model import MODEL_REVISION
from timesfm_serve import weather_catalog

CASES = [f"{station}_20260818T0600Z" for station in ("42410099999", "43128599999", "43279099999")]


def pack(files, output):
    with output.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|") as archive:
            for name, path in sorted(files):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = path.stat().st_size, 0o644, 0
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
    with output.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    replay_files = []
    for case in CASES:
        directory, manifest, _ = weather_catalog.case_manifest(case)
        for name in ("manifest.json", "model_input.csv", "guidance.csv"):
            path = directory / name
            if name != "manifest.json" and hashlib.sha256(path.read_bytes()).hexdigest() != manifest["artifact_sha256"][name]:
                raise ValueError("Replay artifact integrity failure")
            replay_files.append((f"{case}/{name}", path))
    root = args.cache_dir.resolve()
    snapshot = root / "models--google--timesfm-3.0-pytorch" / "snapshots" / MODEL_REVISION
    if not snapshot.is_dir():
        raise ValueError("Pinned model snapshot is missing")
    model_files = []
    for path in snapshot.rglob("*"):
        if path.is_file():
            if not path.resolve().is_relative_to(root):
                raise ValueError("Model cache symlink escapes cache root")
            model_files.append((str(path.relative_to(root)), path))
    if not model_files:
        raise ValueError("Model snapshot is empty")
    result = {}
    for kind, files in (("replays", replay_files), ("models", model_files)):
        archive = args.output / f"{kind}.tar.gz"
        if archive.exists():
            raise ValueError(f"Refusing to overwrite {archive}")
        digest = pack(files, archive)
        result[kind] = {"key": f"{kind}/{digest}.tar.gz", "sha256": digest, "local_file": str(archive), "bytes": archive.stat().st_size}
    (args.output / "artifacts.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"artifacts": result, "model_revision": MODEL_REVISION, "uploaded": False}, indent=2))


if __name__ == "__main__":
    main()
