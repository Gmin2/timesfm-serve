#!/usr/bin/env bash
# build the gpu image for amd64, push to ecr, apply the eks overlay.
# assumes the cluster from k8s/eks-cluster.yaml exists and kubectl points at it.
set -euo pipefail

REGION=${REGION:-us-east-1}
REPO=${REPO:-timesfm-serve}
TAG=${TAG:-gpu}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$REPO" --region "$REGION" >/dev/null

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

docker buildx build --platform linux/amd64 --build-arg TORCH=gpu -t "$REGISTRY/$REPO:$TAG" --push .

cd k8s/overlays/eks
kubectl kustomize --load-restrictor LoadRestrictionsNone . \
  | sed "s#ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/timesfm-serve:gpu#$REGISTRY/$REPO:$TAG#" \
  | kubectl apply -f -

kubectl -n tfm rollout status deploy/api --timeout=600s
kubectl -n tfm rollout status deploy/worker --timeout=900s
