"""Render Kubernetes JSON from explicit deployment inputs. Never calls kubectl or AWS."""

import argparse
import copy
import hashlib
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

NAMESPACE = "weather"
SECURITY = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
# Every alert reads a metric the API already exports, so nothing here needs an agent
# on the GPU node. Queue age covers a dead or descheduled worker; scrape absence
# covers being blind, which is the failure nobody notices on their own.
ALERTS = [
    {"alert": "WeatherMetricsAbsent", "expr": "absent(weather_jobs)", "for": "10m",
     "labels": {"severity": "critical"},
     "annotations": {"summary": "The weather API is not being scraped; every other alert here is silent."}},
    {"alert": "WeatherQueueStalled", "expr": "weather_oldest_pending_seconds > 900", "for": "10m",
     "labels": {"severity": "critical"},
     "annotations": {"summary": "A job has been queued or running for over 15 minutes; the GPU worker is likely gone or wedged."}},
    {"alert": "WeatherJobFailures", "expr": "delta(weather_jobs{status=\"failed\"}[1h]) > 0", "for": "5m",
     "labels": {"severity": "warning"},
     "annotations": {"summary": "A job reached terminal failure in the last hour; check for repeated lease expiry."}},
    {"alert": "WeatherIngestionRejected", "expr": "min_over_time(weather_ingestion_ok[1h]) == 0", "for": "15m",
     "labels": {"severity": "warning"},
     "annotations": {"summary": "Live ingestion for {{ $labels.station_id }} has been rejected for an hour; live reads will expire."}},
    {"alert": "WeatherApiServerErrors", "expr": "sum(rate(weather_http_requests_total{status=~\"5..\"}[5m])) > 0", "for": "5m",
     "labels": {"severity": "warning"},
     "annotations": {"summary": "The weather API is returning 5xx responses."}},
    {"alert": "WeatherApiLatencyHigh",
     "expr": "histogram_quantile(0.99, sum by (le) (rate(weather_http_request_seconds_bucket[5m]))) > 1", "for": "10m",
     "labels": {"severity": "warning"},
     "annotations": {"summary": "API p99 latency is over one second."}},
]


def resource(kind, name, spec=None, api_version="v1", **extra):
    result = {"apiVersion": api_version, "kind": kind, "metadata": {"name": name, "namespace": NAMESPACE}, **extra}
    if spec is not None:
        result["spec"] = spec
    return result


def validate(config, example=False):
    if config.get("example") and not example:
        raise ValueError("Example configuration is for offline validation only; pass --example")
    if "dashboard_origin" in config:
        origin = urlsplit(config["dashboard_origin"])
        if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                or origin.path not in ("", "/") or origin.query or origin.fragment):
            raise ValueError("Dashboard origin must be an HTTPS origin without a path")
    infrastructure = config["infrastructure"]
    if infrastructure["region"] != "us-east-1":
        raise ValueError("Only the reviewed us-east-1 pilot is supported")
    for name in ("api", "worker", "ingest", "bootstrap"):
        expected = infrastructure["repositories"][name]
        if not re.fullmatch(re.escape(expected) + r"@sha256:[a-f0-9]{64}", config["images"][name]):
            raise ValueError("Use digest-pinned images from the matching Terraform ECR repository")
    for name in ("replays", "models"):
        item = config["artifacts"][name]
        if not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) or item["key"] != f"{name}/{item['sha256']}.tar.gz":
            raise ValueError("Artifacts must be content-addressed and checksum-pinned")
    if infrastructure["gpu_nodes"] not in (0, 1):
        raise ValueError("Only zero or one GPU node is supported")
    if learning := config.get("learning"):
        if set(learning) != {"training_mode", "max_seconds", "max_steps"} or learning["training_mode"] not in ("off", "research"):
            raise ValueError("Learning requires an explicit off/research mode and bounded time/steps")
        if not 30 <= learning["max_seconds"] <= 900 or not 1 <= learning["max_steps"] <= 256:
            raise ValueError("Learning exceeds the single-GPU budget")
        if not infrastructure.get("learning_enabled"):
            raise ValueError("Apply the reviewed training artifact permissions first")
    if autoscaling := config.get("autoscaling"):
        if set(autoscaling) != {"cooldown_seconds", "trigger_authentication"}:
            raise ValueError("Autoscaling requires a cooldown and an out-of-band KEDA TriggerAuthentication name")
        if not 300 <= autoscaling["cooldown_seconds"] <= 3600:
            raise ValueError("A GPU cooldown below five minutes reloads the model faster than it serves")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,61}[a-z0-9]", autoscaling["trigger_authentication"]):
            raise ValueError("Invalid TriggerAuthentication name")
        # Scaling the worker to zero deletes the supervisor that owns the training
        # window, so the weekly run would silently never happen again.
        if config.get("learning"):
            raise ValueError("Enable autoscaling or learning, not both; a scaled-to-zero worker has no training supervisor")
    if metrics := config.get("metrics"):
        if set(metrics) != {"secret_arn", "scrape_secret"}:
            raise ValueError("Metrics requires only a token secret ARN and the scrape secret name")
        account = infrastructure["runtime_secrets"]["api"].split(":")[4]
        if not re.fullmatch(rf"arn:aws:secretsmanager:us-east-1:{account}:secret:[A-Za-z0-9/_+=.@-]+", metrics["secret_arn"]):
            raise ValueError("Metrics token secret must belong to this account and region")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,61}[a-z0-9]", metrics["scrape_secret"]):
            raise ValueError("Invalid scrape secret name")
    for key in ("database_cidrs", "s3_cidrs", "secrets_endpoint_cidrs"):
        cidrs = infrastructure.get(key, [])
        if not cidrs or any(ipaddress.ip_network(c).version != 4 or ipaddress.ip_network(c).prefixlen < 8 for c in cidrs):
            raise ValueError(f"Missing or overly broad {key}; refresh Terraform deployment outputs")
    if oauth := config.get("github_oauth"):
        if set(oauth) != {"client_id", "secret_arn", "egress_cidrs"} or not config.get("dashboard_origin"):
            raise ValueError("GitHub OAuth requires a dashboard origin and only client_id, secret_arn, egress_cidrs")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", oauth["client_id"]):
            raise ValueError("Invalid GitHub client ID")
        account = infrastructure["runtime_secrets"]["api"].split(":")[4]
        if not re.fullmatch(rf"arn:aws:secretsmanager:us-east-1:{account}:secret:[A-Za-z0-9/_+=.@-]+", oauth["secret_arn"]):
            raise ValueError("GitHub secret ARN must belong to this account and region")
        api_origin = urlsplit(infrastructure["api_url"])
        if (api_origin.scheme != "https" or not api_origin.hostname or api_origin.username or api_origin.password
                or api_origin.path not in ("", "/") or api_origin.query or api_origin.fragment):
            raise ValueError("Public API origin must be HTTPS without a path")
        networks = [ipaddress.ip_network(cidr) for cidr in oauth["egress_cidrs"]]
        if not 1 <= len(networks) <= 100 or any(n.version != 4 or n.prefixlen < 20 or not n.is_global for n in networks):
            raise ValueError("GitHub egress requires explicit public IPv4 ranges from GitHub metadata")


def pod(config, workload, command=None):
    infrastructure, images = config["infrastructure"], config["images"]
    migrate = workload == "migrate"
    artifacts = "worker" if workload == "worker" else "replays" if workload == "api" else "none"
    secret = infrastructure["admin_secret_arn"] if migrate else infrastructure["runtime_secrets"][workload]
    main = {
        "name": workload, "image": images["bootstrap" if migrate else workload], "imagePullPolicy": "IfNotPresent",
        "securityContext": copy.deepcopy(SECURITY), "envFrom": [{"configMapRef": {"name": "weather-runtime"}}],
        "volumeMounts": [{"name": "credentials", "mountPath": "/run/weather", "readOnly": True}, {"name": "tmp", "mountPath": "/tmp"}],
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
    }
    if command:
        main["command"] = command
    init = {
        "name": "prepare", "image": images["bootstrap"], "imagePullPolicy": "IfNotPresent", "securityContext": copy.deepcopy(SECURITY),
        "command": ["python", "-m", "scripts.weather_cloud_init", "--artifacts", artifacts],
        "envFrom": [{"configMapRef": {"name": "weather-runtime"}}],
        "env": [{"name": "DATABASE_SECRET_ARN", "value": secret}],
        "volumeMounts": [{"name": "credentials", "mountPath": "/run/weather"}, {"name": "tmp", "mountPath": "/tmp"}],
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
    }
    if workload == "api" and (oauth := config.get("github_oauth")):
        init["env"].append({"name": "GITHUB_CLIENT_SECRET_ARN", "value": oauth["secret_arn"]})
    if workload == "api" and (observability := config.get("metrics")):
        init["env"].append({"name": "WEATHER_METRICS_TOKEN_ARN", "value": observability["secret_arn"]})
    if artifacts != "none":
        init["volumeMounts"].append({"name": "assets", "mountPath": "/assets"})
        main["volumeMounts"].append({"name": "assets", "mountPath": "/assets", "readOnly": True})
    return {
        "serviceAccountName": f"weather-{workload}", "automountServiceAccountToken": False,
        "nodeSelector": {"workload": "gpu" if workload == "worker" else "standard", "kubernetes.io/arch": "amd64"},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
        "terminationGracePeriodSeconds": 110 if workload == "worker" else 45,
        "initContainers": [init], "containers": [main],
        "volumes": [{"name": "credentials", "emptyDir": {"medium": "Memory", "sizeLimit": "2Mi"}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}, {"name": "assets", "emptyDir": {"sizeLimit": "4Gi"}}],
    }


def render(config, enable_worker=False, enable_live=False, example=False):
    validate(config, example)
    infrastructure = config["infrastructure"]
    if enable_worker and infrastructure["gpu_nodes"] != 1:
        raise ValueError("GPU worker requires an explicitly provisioned GPU node")
    if enable_live and not enable_worker:
        raise ValueError("Do not schedule live jobs without an enabled worker")
    if config.get("learning") and not (enable_worker and enable_live):
        raise ValueError("Learning requires the existing live, single-GPU worker")
    if config.get("autoscaling") and not enable_worker:
        raise ValueError("Autoscaling scales the existing GPU worker; enable it first")
    release = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
    namespace = resource("Namespace", NAMESPACE)
    namespace["metadata"] = {"name": NAMESPACE, "labels": {"pod-security.kubernetes.io/enforce": "restricted", "pod-security.kubernetes.io/enforce-version": "v1.35"}}
    settings = {
        "AWS_REGION": infrastructure["region"], "AWS_DEFAULT_REGION": infrastructure["region"], "AWS_EC2_METADATA_DISABLED": "true",
        "DATABASE_AUTO_MIGRATE": "0", "DATABASE_HOST": infrastructure["database_host"], "DATABASE_NAME": infrastructure["database_name"],
        "DATABASE_SECRET_FILE": "/run/weather/database.json", "DATABASE_SSL_ROOT_CERT": "/run/weather/root.pem",
        "PGCONNECT_TIMEOUT": "5", "PGOPTIONS": "-c statement_timeout=10000 -c lock_timeout=5000", "DB_POOL_MAX": "5",
        "ARTIFACT_BUCKET": infrastructure["artifact_bucket"],
        "REPLAY_KEY": config["artifacts"]["replays"]["key"], "REPLAY_SHA256": config["artifacts"]["replays"]["sha256"],
        "MODEL_KEY": config["artifacts"]["models"]["key"], "MODEL_SHA256": config["artifacts"]["models"]["sha256"],
        "WEATHER_REPLAY_ROOT": "/assets/replays", "DEVICE": "cuda", "HF_HUB_CACHE": "/assets/hub", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "RUNTIME_SECRET_ARNS": json.dumps(infrastructure["runtime_secrets"], sort_keys=True),
    }
    bootstrap = [namespace, resource("ConfigMap", "weather-runtime", data=settings)]
    bootstrap.extend(resource("ServiceAccount", f"weather-{name}", automountServiceAccountToken=False) for name in ("api", "worker", "ingest", "migrate"))
    bootstrap.append(resource("NetworkPolicy", "weather-boundaries", {
        "podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [],
        "egress": [
            {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}], "ports": [{"protocol": p, "port": 53} for p in ("UDP", "TCP")]},
            {"to": [{"ipBlock": {"cidr": c}} for c in infrastructure["database_cidrs"]], "ports": [{"protocol": "TCP", "port": 5432}]},
            {"to": [{"ipBlock": {"cidr": "169.254.170.23/32"}}], "ports": [{"protocol": "TCP", "port": 80}]},
            {"to": [{"ipBlock": {"cidr": c}} for c in infrastructure["s3_cidrs"] + infrastructure["secrets_endpoint_cidrs"]], "ports": [{"protocol": "TCP", "port": 443}]},
        ],
    }, "networking.k8s.io/v1"))
    bootstrap.append(resource("NetworkPolicy", "weather-ingest-sources", {
        "podSelector": {"matchLabels": {"app": "weather-ingest"}}, "policyTypes": ["Egress"],
        "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]}}],
                    "ports": [{"protocol": "TCP", "port": 443}]}],
    }, "networking.k8s.io/v1"))
    bootstrap.append(resource("NetworkPolicy", "weather-api-ingress", {
        "podSelector": {"matchLabels": {"app": "weather-api"}}, "policyTypes": ["Ingress"],
        "ingress": [{"from": [{"ipBlock": {"cidr": "10.42.0.0/16"}}], "ports": [{"protocol": "TCP", "port": 8000}]}],
    }, "networking.k8s.io/v1"))
    if oauth := config.get("github_oauth"):
        bootstrap.append(resource("NetworkPolicy", "weather-api-github", {
            "podSelector": {"matchLabels": {"app": "weather-api"}}, "policyTypes": ["Egress"],
            "egress": [{"to": [{"ipBlock": {"cidr": cidr}} for cidr in oauth["egress_cidrs"]],
                        "ports": [{"protocol": "TCP", "port": 443}]}],
        }, "networking.k8s.io/v1"))

    migration = pod(config, "migrate", ["python", "-m", "scripts.weather_db_admin"])
    migration["restartPolicy"] = "Never"
    migration_job = resource("Job", f"weather-migrate-{release}", {
        "backoffLimit": 1, "activeDeadlineSeconds": 600, "ttlSecondsAfterFinished": 86400,
        "template": {"metadata": {"labels": {"app": "weather-migrate"}}, "spec": migration},
    }, "batch/v1")

    api = pod(config, "api")
    container = api["containers"][0]
    if "dashboard_origin" in config:
        container["env"] = [{"name": "WEATHER_DASHBOARD_ORIGIN", "value": config["dashboard_origin"].rstrip("/")}]
    if oauth := config.get("github_oauth"):
        container["env"].extend([
            {"name": "GITHUB_CLIENT_ID", "value": oauth["client_id"]},
            {"name": "GITHUB_CLIENT_SECRET_FILE", "value": "/run/weather/github-client-secret"},
            {"name": "WEATHER_PUBLIC_API_ORIGIN", "value": infrastructure["api_url"].rstrip("/")},
        ])
    if config.get("metrics"):
        container.setdefault("env", []).append({"name": "WEATHER_METRICS_TOKEN_FILE", "value": "/run/weather/metrics-token"})
    container["ports"] = [{"name": "http", "containerPort": 8000}]
    container["readinessProbe"] = {"httpGet": {"path": "/health/ready", "port": "http"}, "periodSeconds": 5, "timeoutSeconds": 2}
    container["livenessProbe"] = {"httpGet": {"path": "/health/live", "port": "http"}, "periodSeconds": 15, "timeoutSeconds": 2}
    container["startupProbe"] = {"httpGet": {"path": "/health/live", "port": "http"}, "periodSeconds": 5, "failureThreshold": 24}
    container["lifecycle"] = {"preStop": {"exec": {"command": ["python", "-c", "import time; time.sleep(5)"]}}}
    api["topologySpreadConstraints"] = [{"maxSkew": 1, "topologyKey": "kubernetes.io/hostname", "whenUnsatisfiable": "DoNotSchedule", "labelSelector": {"matchLabels": {"app": "weather-api"}}, "matchLabelKeys": ["pod-template-hash"]}]
    worker = pod(config, "worker")
    worker["tolerations"] = [{"key": "workload", "operator": "Equal", "value": "gpu", "effect": "NoSchedule"}]
    container = worker["containers"][0]
    container["resources"] = {"requests": {"cpu": "2", "memory": "6Gi", "nvidia.com/gpu": 1}, "limits": {"cpu": "3", "memory": "12Gi", "nvidia.com/gpu": 1}}
    check = {"exec": {"command": ["python", "-m", "timesfm_serve.weather_worker", "--check-ready"]}, "timeoutSeconds": 5, "periodSeconds": 15}
    container["startupProbe"] = {**copy.deepcopy(check), "failureThreshold": 40}
    container["readinessProbe"] = {**copy.deepcopy(check), "failureThreshold": 1}
    container["livenessProbe"] = {**copy.deepcopy(check), "failureThreshold": 2}
    if learning := config.get("learning"):
        container["command"] = ["python", "-m", "timesfm_serve.weather_gpu", "--max-seconds", str(learning["max_seconds"]),
                                "--max-steps", str(learning["max_steps"])]
        container["env"] = [{"name": "WEATHER_TRAINING_MODE", "value": learning["training_mode"]}]
        container["volumeMounts"].append({"name": "learning", "mountPath": "/learning"})
        worker["volumes"].append({"name": "learning", "emptyDir": {"sizeLimit": "128Mi"}})

    deployments = []
    for name, spec, replicas in (("api", api, 2), ("worker", worker, int(enable_worker))):
        labels = {"app": f"weather-{name}"}
        strategy = {"type": "Recreate"} if name == "worker" else {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}
        annotations = {"weather/release": release}
        if name != "worker" or not config.get("learning"):
            annotations["eks.amazonaws.com/skip-containers"] = name
        deployments.append(resource("Deployment", f"weather-{name}", {
            "replicas": replicas, "strategy": strategy, "revisionHistoryLimit": 3, "progressDeadlineSeconds": 1200,
            "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels, "annotations": annotations}, "spec": spec},
        }, "apps/v1"))
    ingestion = pod(config, "ingest")
    ingestion["restartPolicy"] = "Never"
    # Scoring is SQL and numpy. It reuses the ingest identity so it needs no new IAM,
    # and it runs on a standard node so the GPU is never held open for it.
    scoring = pod(config, "ingest", ["python", "-m", "scripts.weather_score"])
    scoring["restartPolicy"] = "Never"
    service = resource("Service", "weather-api", {"type": "NodePort", "externalTrafficPolicy": "Cluster", "selector": {"app": "weather-api"}, "ports": [{"name": "http", "port": 80, "targetPort": "http", "nodePort": 30080}]})
    service["metadata"]["labels"] = {"app": "weather-api"}
    deployments.extend([
        service,
        resource("PodDisruptionBudget", "weather-api", {"minAvailable": 1, "selector": {"matchLabels": {"app": "weather-api"}}}, "policy/v1"),
        resource("CronJob", "weather-ingest", {
            "schedule": "*/5 * * * *", "timeZone": "Etc/UTC", "suspend": not enable_live, "concurrencyPolicy": "Forbid", "startingDeadlineSeconds": 120,
            "successfulJobsHistoryLimit": 2, "failedJobsHistoryLimit": 3,
            "jobTemplate": {"spec": {"backoffLimit": 1, "activeDeadlineSeconds": 300, "ttlSecondsAfterFinished": 86400,
                "template": {"metadata": {"labels": {"app": "weather-ingest"}, "annotations": {
                    "eks.amazonaws.com/skip-containers": "ingest",
                }}, "spec": ingestion}}},
        }, "batch/v1"),
        resource("CronJob", "weather-score", {
            "schedule": "17 * * * *", "timeZone": "Etc/UTC", "suspend": not enable_live, "concurrencyPolicy": "Forbid", "startingDeadlineSeconds": 300,
            "successfulJobsHistoryLimit": 2, "failedJobsHistoryLimit": 3,
            "jobTemplate": {"spec": {"backoffLimit": 1, "activeDeadlineSeconds": 900, "ttlSecondsAfterFinished": 86400,
                "template": {"metadata": {"labels": {"app": "weather-score"}, "annotations": {
                    "eks.amazonaws.com/skip-containers": "ingest",
                }}, "spec": scoring}}},
        }, "batch/v1"),
    ])
    if autoscaling := config.get("autoscaling"):
        deployments.append(resource("ScaledObject", "weather-worker", {
            "scaleTargetRef": {"name": "weather-worker"},
            "pollingInterval": 30, "cooldownPeriod": autoscaling["cooldown_seconds"],
            "idleReplicaCount": 0, "minReplicaCount": 1, "maxReplicaCount": 1,
            "advanced": {"restoreToOriginalReplicaCount": False},
            "triggers": [{"type": "postgresql", "metadata": {
                "query": "select count(*) from weather_jobs where status in ('queued','running')",
                "targetQueryValue": "1", "activationTargetQueryValue": "0",
            }, "authenticationRef": {"name": autoscaling["trigger_authentication"]}}],
        }, "keda.sh/v1alpha1"))
    if observability := config.get("metrics"):
        deployments.append(resource("ServiceMonitor", "weather-api", {
            "selector": {"matchLabels": {"app": "weather-api"}},
            "namespaceSelector": {"matchNames": [NAMESPACE]},
            "endpoints": [{"port": "http", "path": "/metrics", "interval": "30s", "scrapeTimeout": "10s", "scheme": "http",
                           "authorization": {"type": "Bearer", "credentials": {"name": observability["scrape_secret"], "key": "token"}}}],
        }, "monitoring.coreos.com/v1"))
        deployments.append(resource("PrometheusRule", "weather", {"groups": [{"name": "weather", "rules": ALERTS}]}, "monitoring.coreos.com/v1"))
    operator = pod(config, "migrate", ["python", "-c", "import time; time.sleep(600)"])
    operator.update({"restartPolicy": "Never", "activeDeadlineSeconds": 600})
    return {
        "01-bootstrap.json": bootstrap, "02-migrate.json": [migration_job], "03-workloads.json": deployments,
        "04-operator.json": [resource("Pod", "weather-operator", operator)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--example", action="store_true")
    parser.add_argument("--enable-worker", action="store_true")
    parser.add_argument("--enable-live", action="store_true")
    args = parser.parse_args()
    documents = render(json.loads(args.config.read_text()), args.enable_worker, args.enable_live, args.example)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, items in documents.items():
        (args.output / name).write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2) + "\n")
    print(json.dumps({"rendered": list(documents), "applied": False, "worker_enabled": args.enable_worker, "live_enabled": args.enable_live}))


if __name__ == "__main__":
    main()
