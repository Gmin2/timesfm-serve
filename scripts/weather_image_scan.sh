#!/usr/bin/env bash
set -euo pipefail

image="${1:?Supply the image to scan}"
output="${2:?Supply a report directory}"
mkdir -p "$output"
trivy image --disable-telemetry --no-progress --scanners vuln --pkg-types os,library \
  --format cyclonedx --output "$output/sbom.cdx.json" "$image"
trivy image --disable-telemetry --no-progress --scanners vuln --pkg-types os,library \
  --vuln-severity-source nvd,ghsa,auto --severity HIGH,CRITICAL \
  --ignorefile /dev/null --ignore-unfixed=false --exit-code 1 --exit-on-eol 1 \
  --format json --output "$output/vulnerabilities.json" "$image"
