#!/usr/bin/env bash
# one time: create the cluster, install the nvidia device plugin, then run deploy_eks.sh
set -euo pipefail
eksctl create cluster -f k8s/eks-cluster.yaml
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.1/deployments/static/nvidia-device-plugin.yml
kubectl -n kube-system rollout status ds/nvidia-device-plugin-daemonset --timeout=300s
