"""Operator-only migrations and role provisioning; runtime roles cannot perform DDL."""

import json
import os
import secrets

from psycopg import sql

from scripts.weather_cloud_init import aws_client

GRANTS = {
    "api": {
        "SELECT": ["accounts", "api_keys", "weather_jobs", "weather_forecasts", "weather_live_inputs", "weather_ingestion_status", "schema_migrations"],
        "INSERT": ["weather_jobs", "api_keys"],
        "SELECT, INSERT, UPDATE, DELETE": ["rate_limit_counters"],
        "SELECT, INSERT, DELETE": ["dashboard_sessions", "github_oauth_attempts"],
        "INSERT (name, github_id, github_login, credits_granted)": ["accounts"],
        "UPDATE (credits_used, github_login)": ["accounts"],
        "UPDATE (revoked_at)": ["api_keys"],
    },
    "worker": {
        "SELECT": ["accounts", "weather_jobs", "weather_forecasts", "weather_live_inputs", "schema_migrations"],
        "UPDATE": ["weather_jobs"],
        "INSERT": ["weather_forecasts"],
        "UPDATE (credits_used)": ["accounts"],
        "SELECT, INSERT, UPDATE": ["weather_evaluations", "weather_training_runs"],
    },
    "ingest": {
        "SELECT": ["weather_jobs", "weather_live_inputs", "schema_migrations"],
        "INSERT": ["weather_jobs", "weather_live_inputs"],
        "SELECT, INSERT, UPDATE": ["weather_ingestion_status"],
    },
}


def provision_role(connection, role, password, workload):
    exists = connection.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone()
    if not exists:
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION").format(sql.Identifier(role)))
    connection.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
    for privileges, tables in GRANTS[workload].items():
        connection.execute(sql.SQL("GRANT {} ON {} TO {}").format(
            sql.SQL(privileges), sql.SQL(", ").join(sql.Identifier("public", t) for t in tables), sql.Identifier(role),
        ))
    if workload == "api":
        connection.execute(sql.SQL("GRANT USAGE ON SEQUENCE public.accounts_id_seq, public.api_keys_id_seq TO {}").format(sql.Identifier(role)))


def main():
    from timesfm_serve import db

    client = aws_client("secretsmanager")
    arns = json.loads(os.environ["RUNTIME_SECRET_ARNS"])
    if set(arns) != set(GRANTS):
        raise ValueError("Expected exactly api, worker and ingest secret ARNs")
    credentials, missing = {}, set()
    for workload, arn in arns.items():
        try:
            credentials[workload] = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
        except client.exceptions.ResourceNotFoundException:
            credentials[workload] = {"username": f"weather_{workload}", "password": secrets.token_urlsafe(36)}
            missing.add(workload)
        value = credentials[workload]
        if value.get("username") != f"weather_{workload}" or not isinstance(value.get("password"), str) or len(value["password"]) < 32:
            raise ValueError("Unexpected runtime secret identity or password")
    db.migrate()
    try:
        with db.conn() as c, c.transaction():
            c.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            for workload, value in credentials.items():
                provision_role(c, value["username"], value["password"], workload)
        # Runtime pods are released only after this job succeeds. An interrupted
        # initial run can be repeated; existing secret values are never rotated.
        for workload in missing:
            client.put_secret_value(SecretId=arns[workload], SecretString=json.dumps(credentials[workload]))
    finally:
        db.pool.close()
    print(json.dumps({"event": "weather_database_prepared", "roles": sorted(GRANTS)}))


if __name__ == "__main__":
    main()
