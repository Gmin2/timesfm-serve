#!/usr/bin/env bash
set -euo pipefail

directory="${1:?Supply a tools directory}"
[[ "$(uname -s)" == "Linux" && "$(uname -m)" == "x86_64" ]]
mkdir -p "$directory"

install_tool() {
  local repository="$1" version="$2" archive="$3" checksum="$4" binary="$5"
  curl --fail --silent --show-error --location --retry 3 \
    "https://github.com/${repository}/releases/download/${version}/${archive}" -o "${directory}/${archive}"
  printf '%s  %s\n' "$checksum" "${directory}/${archive}" | sha256sum --check --strict
  tar -xzf "${directory}/${archive}" -C "$directory" "$binary"
}

install_tool aquasecurity/trivy v0.74.0 trivy_0.74.0_Linux-64bit.tar.gz \
  2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a trivy
install_tool gitleaks/gitleaks v8.30.1 gitleaks_8.30.1_linux_x64.tar.gz \
  551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb gitleaks
