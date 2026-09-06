"""Package allowlisted backend build inputs; never includes local configuration or secrets."""

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    names = {"pyproject.toml", "uv.lock", ".dockerignore", "deploy/rds-global-bundle.pem"}
    for pattern in ("timesfm_serve/*.py", "scripts/*.py", "scripts/*.sh", "migrations/*.sql", "deploy/Dockerfile.*"):
        names.update(str(p.relative_to(ROOT)) for p in ROOT.glob(pattern))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(names):
            file = ROOT / name
            if file.is_symlink() or not file.is_file():
                raise ValueError("Build inputs must be regular files")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, file.read_bytes())
    payload = output.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / f"{digest}.zip"
    target.write_bytes(payload)
    print(json.dumps({"path": str(target), "key": f"builds/{digest}.zip", "sha256": digest, "files": len(names), "bytes": len(payload)}))


if __name__ == "__main__":
    main()
