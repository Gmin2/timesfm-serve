import os
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from scripts.weather_ecr_gate import check_scan
from timesfm_serve import auth


@pytest.mark.parametrize("scan_status", [0, 1, 2])
def test_image_gate_preserves_scanner_failure(tmp_path, scan_status):
    scanner = tmp_path / "trivy"
    scanner.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SCAN_CALLS"\n'
        'case "$*" in *"--format cyclonedx"*) exit 0;; *) exit "$SCAN_STATUS";; esac\n'
    )
    scanner.chmod(0o755)
    calls = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "scripts/weather_image_scan.sh", "example@sha256:" + "a" * 64, str(tmp_path / "reports")],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "SCAN_CALLS": str(calls), "SCAN_STATUS": str(scan_status)},
        capture_output=True, text=True,
    )
    assert result.returncode == scan_status
    invocations = calls.read_text().splitlines()
    assert len(invocations) == 2
    assert "--format cyclonedx" in invocations[0]
    assert "--ignore-unfixed=false" in invocations[1]
    assert "--ignorefile /dev/null" in invocations[1]
    assert "--severity HIGH,CRITICAL" in invocations[1]
    assert "--vuln-severity-source nvd,ghsa,auto" in invocations[1]


def test_image_scan_precedes_registry_push():
    script = Path("scripts/weather_build_images.sh").read_text()
    assert script.index("bash scripts/weather_image_scan.sh") < script.index('docker push "$image"')


@pytest.mark.parametrize("key", ["invalid", "tfm_short", "tfm_" + "x" * 33, "tfm_" + "x" * 31 + ".", "x" * 10000, "' OR 1=1 --"])
def test_malformed_keys_never_query_database(monkeypatch, key):
    def forbidden(*args):
        pytest.fail("Malformed API key reached the database")

    monkeypatch.setattr(auth.db, "account_for_key_hash", forbidden)
    with pytest.raises(HTTPException) as error:
        auth.require_key(Request({"type": "http"}), x_api_key=key)
    assert error.value.status_code == 401


def test_well_formed_unknown_key_still_requires_database_validation(monkeypatch):
    lookups = []
    monkeypatch.setattr(auth.db, "account_for_key_hash", lambda value: lookups.append(value))
    with pytest.raises(HTTPException) as error:
        auth.require_key(Request({"type": "http"}), x_api_key="tfm_" + "a" * 32)
    assert error.value.status_code == 401
    assert lookups == [auth.hash_key("tfm_" + "a" * 32)]


@pytest.mark.parametrize("counts", [{}, {"LOW": 3}, {"MEDIUM": 1}])
def test_ecr_gate_allows_complete_low_severity_scans(counts):
    check_scan({"imageId": {"imageDigest": "sha256:example"}, "imageScanStatus": {"status": "COMPLETE"},
                "imageScanFindings": {"findingSeverityCounts": counts}}, "sha256:example")


@pytest.mark.parametrize(("digest", "status", "counts"), [
    ("different", "COMPLETE", {}), ("sha256:example", "IN_PROGRESS", {}),
    ("sha256:example", "COMPLETE", {"CRITICAL": 1}), ("sha256:example", "COMPLETE", {"HIGH": 1}),
    ("sha256:example", "COMPLETE", None), ("sha256:example", "COMPLETE", {"HIGH": -1}),
])
def test_ecr_gate_fails_closed(digest, status, counts):
    with pytest.raises(ValueError):
        check_scan({"imageId": {"imageDigest": digest}, "imageScanStatus": {"status": status},
                    "imageScanFindings": {"findingSeverityCounts": counts}}, "sha256:example")
