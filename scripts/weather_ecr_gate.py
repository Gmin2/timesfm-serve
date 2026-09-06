"""Reject incomplete, mismatched or high/critical ECR scan results."""

import argparse
import json
from pathlib import Path


def check_scan(report, digest):
    if report.get("imageId", {}).get("imageDigest") != digest:
        raise ValueError("ECR scan image does not match the release digest")
    if report.get("imageScanStatus", {}).get("status") != "COMPLETE":
        raise ValueError("ECR image scan is not complete")
    counts = report.get("imageScanFindings", {}).get("findingSeverityCounts")
    if not isinstance(counts, dict) or any(type(v) is not int or v < 0 for v in counts.values()):
        raise ValueError("ECR scan severity counts are missing or invalid")
    if counts.get("CRITICAL", 0) or counts.get("HIGH", 0):
        raise ValueError("ECR scan contains unresolved critical/high vulnerabilities")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("digest")
    args = parser.parse_args()
    check_scan(json.loads(args.report.read_text()), args.digest)
    print(json.dumps({"image_digest": args.digest, "ecr_scan_passed": True}))


if __name__ == "__main__":
    main()
