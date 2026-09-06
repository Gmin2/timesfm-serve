#!/usr/bin/env bash
set -euo pipefail

: "${AWS_ACCOUNT_ID:?}"
: "${AWS_DEFAULT_REGION:?}"
: "${PROJECT_NAME:?}"
: "${RELEASE_TAG:?}"
[[ "$RELEASE_TAG" =~ ^[a-f0-9]{64}$ ]]
[[ "$(uname -m)" == "x86_64" ]]
bash scripts/weather_security_tools.sh /tmp/weather-security-tools
export PATH="/tmp/weather-security-tools:$PATH"
if (( $# == 0 )); then
  set -- api ingest bootstrap worker
fi
for component in "$@"; do
  case "$component" in
    api|ingest|bootstrap|worker) ;;
    *) printf 'Unsupported component: %s\n' "$component" >&2; exit 2 ;;
  esac
done

registry="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_DEFAULT_REGION}.amazonaws.com"
export DOCKER_BUILDKIT=1
export DOCKER_CONFIG
DOCKER_CONFIG="$(mktemp -d)"
trap 'rm -rf "$DOCKER_CONFIG"' EXIT
aws ecr get-login-password --region "$AWS_DEFAULT_REGION" |
  docker login --username AWS --password-stdin "$registry"

for component in "$@"; do
  image="${registry}/${PROJECT_NAME}/${component}:${RELEASE_TAG}"
  docker build --platform linux/amd64 -f "deploy/Dockerfile.${component}" -t "$image" .
  docker run --rm --read-only --tmpfs /tmp "$image" python -c \
    'import os, platform; assert os.getuid() == 10001; assert platform.machine() == "x86_64"'
  if [[ "$component" == "worker" ]]; then
    docker run --rm --read-only --tmpfs /tmp "$image" python -c \
      'import torch; from timesfm_serve.weather_engine import WeatherEngine; assert torch.version.cuda is not None; print({"torch": torch.__version__, "cuda_build": torch.version.cuda, "gpu_execution_tested": False})'
  else
    docker run --rm --read-only --tmpfs /tmp "$image" python -c \
      'import importlib.util; assert importlib.util.find_spec("torch") is None'
  fi
  bash scripts/weather_image_scan.sh "$image" "/tmp/weather-security/${component}"
  docker push "$image"
  digest="$(aws ecr describe-images --repository-name "${PROJECT_NAME}/${component}" \
    --image-ids "imageTag=${RELEASE_TAG}" --query 'imageDetails[0].imageDigest' --output text)"
  # Scan-on-push registration can lag the push; never promote without a completed scan.
  for attempt in 1 2 3; do
    if aws ecr wait image-scan-complete --repository-name "${PROJECT_NAME}/${component}" --image-id "imageDigest=${digest}"; then
      break
    fi
    (( attempt < 3 )) || exit 1
    sleep 10
  done
  aws ecr describe-image-scan-findings --repository-name "${PROJECT_NAME}/${component}" \
    --image-id "imageDigest=${digest}" > "/tmp/weather-security/${component}/ecr.json"
  python -m scripts.weather_ecr_gate "/tmp/weather-security/${component}/ecr.json" "$digest"
  printf '%s\n' "$digest"
done
