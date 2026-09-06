import hashlib
import io
import json
import tarfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from scripts.weather_cloud_init import download, unpack_verified, write_private
from scripts.weather_costs import estimate
from scripts.weather_db_admin import provision_role
from scripts.weather_render import render
from timesfm_serve import customer_store, db, weather_catalog, weather_live_store, weather_store
from timesfm_serve.auth import hash_key


@pytest.fixture(scope="session", autouse=True)
def close_deployment_pool():
    yield
    db.pool.close()


def bundle(tmp_path, name="case/input.csv", kind=None):
    path = tmp_path / "bundle.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        if kind:
            member.type, member.linkname = kind, "/etc/passwd"
            archive.addfile(member)
        else:
            member.size = 4
            archive.addfile(member, io.BytesIO(b"data"))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_artifact_hash_and_extraction(tmp_path):
    path, digest = bundle(tmp_path)
    unpack_verified(path, tmp_path / "output", digest)
    assert (tmp_path / "output/case/input.csv").read_bytes() == b"data"
    with pytest.raises(ValueError, match="SHA256"):
        unpack_verified(path, tmp_path / "other", "0" * 64)
    assert not (tmp_path / "other").exists()


def test_private_secret_file_permissions_and_symlinks(tmp_path):
    target = tmp_path / "secret"
    write_private(target, "test-only-secret")
    assert target.read_text() == "test-only-secret"
    assert target.stat().st_mode & 0o777 == 0o600
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        write_private(link, "replacement")
    assert target.read_text() == "test-only-secret"


@pytest.mark.parametrize("valid_digest", [True, False])
def test_s3_download_context_returns_raw_stream(tmp_path, valid_digest):
    path, digest = bundle(tmp_path)
    data = path.read_bytes()
    raw = io.BytesIO(data)

    class StreamingBody:
        def __enter__(self):
            return raw

        def __exit__(self, *args):
            raw.close()

    class S3:
        def get_object(self, **kwargs):
            assert kwargs == {"Bucket": "artifacts", "Key": "replays/pinned.tar.gz"}
            return {"ContentLength": len(data), "Body": StreamingBody()}

    destination = tmp_path / "downloaded"
    if valid_digest:
        download(S3(), "artifacts", "replays/pinned.tar.gz", digest, destination)
        assert (destination / "case/input.csv").read_bytes() == b"data"
    else:
        with pytest.raises(ValueError, match="SHA256"):
            download(S3(), "artifacts", "replays/pinned.tar.gz", "0" * 64, destination)
        assert not (destination / "case").exists()
    assert raw.closed
    assert not (destination / "download.tar.gz").exists()


@pytest.mark.parametrize(("name", "kind"), [("../escape", None), ("/tmp/escape", None), ("link", tarfile.SYMTYPE), ("link", tarfile.LNKTYPE)])
def test_reject_unsafe_artifacts(tmp_path, name, kind):
    path, digest = bundle(tmp_path, name, kind)
    with pytest.raises(ValueError, match="Unsafe"):
        unpack_verified(path, tmp_path / "output", digest)


def test_real_database_runtime_roles(monkeypatch):
    assert conninfo_to_dict(db.DSN).get("dbname", "").endswith("_test"), "Use a disposable *_test database"
    db.migrate()
    original_conn = db.conn
    roles = {w: f"weather_test_{w}_{uuid.uuid4().hex[:8]}" for w in ("api", "worker", "ingest")}
    account = db.find_or_create_account(f"deployment-test-{uuid.uuid4()}")
    github_id = uuid.uuid4().int % 10**15 + 1
    key = db.create_key(account)
    with original_conn() as c:
        assert c.info.dbname.endswith("_test")
        for workload, role in roles.items():
            provision_role(c, role, "test-password-not-used-for-cloud", workload)

    def assume(workload):
        @contextmanager
        def connection():
            with original_conn() as c:
                c.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(roles[workload])))
                try:
                    yield c
                finally:
                    c.execute("RESET ROLE")
        monkeypatch.setattr(db, "conn", connection)

    try:
        assume("api")
        monkeypatch.setenv("DATABASE_AUTO_MIGRATE", "0")
        db.initialize()
        assert db.account_for_key_hash(hash_key(key))[0] == account
        customer_store.save_attempt("state", "browser", "verifier")
        assert customer_store.consume_attempt("state", "browser") == "verifier"
        token = customer_store.github_session(github_id, "example")
        session = customer_store.session_account(token)
        assert customer_store.github_session(github_id, "renamed", token)
        customer_key = customer_store.create_customer_key(session["id"], "Runtime test")
        assert len(customer_store.keys(session["id"])) == 1
        assert db.revoke_key(customer_key["id"], session["id"])
        db.bump_rate_limit(hash_key(key), 60000)
        case = weather_catalog.catalog()[0]
        job_id, created = weather_store.submit(account, "deployment-test", case["case_id"], case["manifest_sha256"])
        assert created and weather_store.get_job(job_id, account)["status"] == "queued"
        assert weather_store.submit(account, "deployment-test", case["case_id"], case["manifest_sha256"]) == (job_id, False)
        with db.conn() as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("CREATE TABLE public.forbidden_runtime_table (id int)")
        with db.conn() as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE weather_jobs SET attempts = 0")
        assume("worker")
        db.initialize()
        weather_store.recover_expired()
        job = weather_store.claim()
        assert job["id"] == job_id
        assert weather_store.fail(job, retryable=False)
        with db.conn() as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM weather_forecasts")
        assume("ingest")
        db.initialize()
        with weather_live_store.station_lock("43128599999") as locked:
            assert locked
        with db.conn() as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM accounts")
        with original_conn() as c:
            assert c.execute("select credits_used from accounts where id = %s", (account,)).fetchone()[0] == 0
    finally:
        monkeypatch.setattr(db, "conn", original_conn)
        with original_conn() as c:
            c.execute("delete from rate_limit_counters where key_hash = %s", (hash_key(key),))
            c.execute("delete from accounts where id = %s", (account,))
            c.execute("delete from accounts where github_id = %s", (str(github_id),))
            for role in roles.values():
                c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def deployment_config():
    return json.loads(Path("deploy/example.json").read_text())


def test_render_defaults_are_dormant_and_restricted():
    documents = render(deployment_config(), example=True)
    workloads = {item["metadata"]["name"]: item for item in documents["03-workloads.json"] if item["kind"] in ("Deployment", "CronJob")}
    assert workloads["weather-worker"]["spec"]["replicas"] == 0
    assert workloads["weather-ingest"]["spec"]["suspend"] is True
    assert workloads["weather-api"]["spec"]["replicas"] == 2
    api = workloads["weather-api"]["spec"]["template"]["spec"]
    assert api["topologySpreadConstraints"][0]["matchLabelKeys"] == ["pod-template-hash"]
    for document in documents["02-migrate.json"] + documents["03-workloads.json"]:
        spec = document["spec"]
        if document["kind"] == "CronJob":
            spec = spec["jobTemplate"]["spec"]
        if "template" not in spec:
            continue
        pod = spec["template"]["spec"]
        assert pod["securityContext"]["runAsNonRoot"]
        assert pod["automountServiceAccountToken"] is False
        for container in pod["containers"] + pod["initContainers"]:
            assert "@sha256:" in container["image"]
            assert container["securityContext"]["readOnlyRootFilesystem"]
            assert not container["securityContext"]["allowPrivilegeEscalation"]
    worker = workloads["weather-worker"]["spec"]["template"]["spec"]
    assert worker["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert worker["nodeSelector"]["workload"] == "gpu"


def test_render_requires_explicit_enabling():
    config = deployment_config()
    with pytest.raises(ValueError, match="Example"):
        render(config)
    with pytest.raises(ValueError, match="GPU node"):
        render(config, enable_worker=True, example=True)
    with pytest.raises(ValueError, match="schedule"):
        render(config, enable_live=True, example=True)
    config["infrastructure"]["gpu_nodes"] = 1
    documents = render(config, enable_worker=True, enable_live=True, example=True)
    workloads = {item["metadata"]["name"]: item for item in documents["03-workloads.json"]}
    assert workloads["weather-worker"]["spec"]["replicas"] == 1
    assert not workloads["weather-ingest"]["spec"]["suspend"]


def test_render_dashboard_origin_is_api_only():
    config = deployment_config()
    config["dashboard_origin"] = "https://timesfms.vercel.app/"
    documents = render(config, example=True)
    for resource in documents["03-workloads.json"]:
        if resource["kind"] != "Deployment":
            continue
        container = resource["spec"]["template"]["spec"]["containers"][0]
        if container["name"] == "api":
            assert container["env"] == [{"name": "WEATHER_DASHBOARD_ORIGIN", "value": "https://timesfms.vercel.app"}]
        else:
            assert "WEATHER_DASHBOARD_ORIGIN" not in {item["name"] for item in container.get("env", [])}
    config["dashboard_origin"] = "https://timesfms.vercel.app/api/auth/github/callback"
    with pytest.raises(ValueError, match="Dashboard origin"):
        render(config, example=True)


def test_render_github_oauth_secret_and_egress_are_api_only():
    config = deployment_config()
    config["infrastructure"]["api_url"] = "https://api.example.invalid"
    config["github_oauth"] = {
        "client_id": "test-client-id",
        "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:weather/github/oauth-ABCDEF",
        "egress_cidrs": ["140.82.112.0/20"],
    }
    documents = render(config, example=True)
    api = next(r for r in documents["03-workloads.json"] if r["kind"] == "Deployment" and r["metadata"]["name"] == "weather-api")
    pod = api["spec"]["template"]["spec"]
    env = {v["name"]: v["value"] for v in pod["containers"][0]["env"]}
    assert env["GITHUB_CLIENT_SECRET_FILE"] == "/run/weather/github-client-secret"
    assert env["WEATHER_PUBLIC_API_ORIGIN"] == "https://api.example.invalid"
    assert "GITHUB_CLIENT_SECRET_ARN" not in env
    assert {v["name"]: v["value"] for v in pod["initContainers"][0]["env"]}["GITHUB_CLIENT_SECRET_ARN"] == config["github_oauth"]["secret_arn"]
    for r in documents["03-workloads.json"]:
        if r["kind"] in ("Deployment", "CronJob") and r["metadata"]["name"] != "weather-api":
            assert "GITHUB_CLIENT" not in json.dumps(r)
    policy = next(r for r in documents["01-bootstrap.json"] if r["metadata"]["name"] == "weather-api-github")
    assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "weather-api"}}
    assert policy["spec"]["egress"][0]["ports"] == [{"protocol": "TCP", "port": 443}]
    config["github_oauth"]["egress_cidrs"] = ["0.0.0.0/0"]
    with pytest.raises(ValueError, match="GitHub egress"):
        render(config, example=True)


def test_runtime_containers_opt_out_of_aws_identity_injection():
    documents = render(deployment_config(), example=True)
    for resource in documents["03-workloads.json"]:
        if resource["kind"] == "Deployment":
            template = resource["spec"]["template"]
        elif resource["kind"] == "CronJob":
            template = resource["spec"]["jobTemplate"]["spec"]["template"]
        else:
            continue
        runtime = template["spec"]["containers"][0]["name"]
        assert template["metadata"]["annotations"]["eks.amazonaws.com/skip-containers"] == runtime
        assert runtime != template["spec"]["initContainers"][0]["name"]
        assert not template["spec"].get("shareProcessNamespace", False)
    migration = documents["02-migrate.json"][0]["spec"]["template"]
    operator = documents["04-operator.json"][0]
    for privileged in (migration, operator):
        assert "eks.amazonaws.com/skip-containers" not in privileged["metadata"].get("annotations", {})


def test_render_rejects_mutable_images_and_artifacts():
    config = deployment_config()
    config["images"]["worker"] = config["infrastructure"]["repositories"]["worker"] + ":latest"
    with pytest.raises(ValueError, match="digest-pinned"):
        render(config, example=True)
    config = deployment_config()
    config["artifacts"]["models"]["key"] = "models/latest.tar.gz"
    with pytest.raises(ValueError, match="content-addressed"):
        render(config, example=True)


def test_only_ingestion_has_public_https_egress():
    config = deployment_config()
    policies = [r for r in render(config, example=True)["01-bootstrap.json"] if r["kind"] == "NetworkPolicy"]
    public = [p for p in policies if any(
        target.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"
        for rule in p["spec"].get("egress", []) for target in rule.get("to", [])
    )]
    assert len(public) == 1
    assert public[0]["spec"]["podSelector"] == {"matchLabels": {"app": "weather-ingest"}}
    config["infrastructure"]["secrets_endpoint_cidrs"] = ["0.0.0.0/0"]
    with pytest.raises(ValueError, match="overly broad"):
        render(config, example=True)
    config["infrastructure"].pop("secrets_endpoint_cidrs")
    with pytest.raises(ValueError, match="refresh Terraform"):
        render(config, example=True)


def test_costs_include_idle_stack_and_gpu_storage():
    idle = estimate()
    assert idle["fixed_subtotal_usd"] == 203.84
    assert not idle["hard_spending_cap"]
    running = estimate(730, 730)
    assert running["fixed_subtotal_usd"] == 797.75
    assert running["usd"]["gpu_disk_80gb"] == 6.4
    with pytest.raises(ValueError):
        estimate(8, 9)
