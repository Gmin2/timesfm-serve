"""Pod Identity init container: private credentials and checksum-pinned S3 artifacts."""

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import time
from pathlib import Path


def aws_client(service):
    import boto3
    from botocore.config import Config

    return boto3.client(service, config=Config(connect_timeout=5, read_timeout=20, retries={"mode": "standard", "total_max_attempts": 3}))


def unpack_verified(archive, destination, expected_sha256):
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != expected_sha256:
        raise ValueError("Artifact SHA256 mismatch")
    with tarfile.open(archive, "r:gz") as bundle:
        members, total_size = [], 0
        root = Path(destination).resolve()
        for member in bundle:
            total_size += member.size
            if len(members) >= 10000 or total_size > 4 * 1024**3:
                raise ValueError("Artifact exceeds extraction limit")
            path = (root / member.name).resolve()
            if (not member.isfile() and not member.isdir()) or not path.is_relative_to(root) or Path(member.name).is_absolute():
                raise ValueError("Unsafe artifact member")
            members.append(member)
        bundle.extractall(root, members=members, filter="data")


def download(s3, bucket, key, sha256, destination):
    archive = Path(destination) / "download.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    response = s3.get_object(Bucket=bucket, Key=key)
    if response["ContentLength"] > 2 * 1024**3:
        response["Body"].close()
        raise ValueError("Artifact exceeds download limit")
    start, size = time.monotonic(), 0
    try:
        with response["Body"] as body, archive.open("wb") as output:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                size += len(chunk)
                if size > 2 * 1024**3 or time.monotonic() - start > 600:
                    raise ValueError("Artifact download limit exceeded")
                output.write(chunk)
        unpack_verified(archive, destination, sha256)
    finally:
        archive.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", choices=("none", "replays", "worker"), default="none")
    args = parser.parse_args()
    secret = aws_client("secretsmanager").get_secret_value(SecretId=os.environ["DATABASE_SECRET_ARN"])["SecretString"]
    payload = json.loads(secret)
    if not all(isinstance(payload.get(k), str) and payload[k] for k in ("username", "password")):
        raise ValueError("Invalid database credential payload")
    directory = Path("/run/weather")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "database.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({k: payload[k] for k in ("username", "password")}, stream)
    shutil.copyfile("/certs/rds-global-bundle.pem", directory / "root.pem")
    if args.artifacts != "none":
        client = aws_client("s3")
        download(client, os.environ["ARTIFACT_BUCKET"], os.environ["REPLAY_KEY"], os.environ["REPLAY_SHA256"], "/assets/replays")
        if args.artifacts == "worker":
            download(client, os.environ["ARTIFACT_BUCKET"], os.environ["MODEL_KEY"], os.environ["MODEL_SHA256"], "/assets/hub")
    print(json.dumps({"event": "weather_pod_initialized", "artifacts": args.artifacts}))


if __name__ == "__main__":
    main()
