import json

import pytest
from psycopg.conninfo import conninfo_to_dict

from timesfm_serve import db
from timesfm_serve.database_config import database_dsn


def test_mounted_credentials_require_verified_tls(tmp_path, monkeypatch):
    secret = tmp_path / "credentials.json"
    secret.write_text(json.dumps({"username": "weather_api", "password": "space ' quote : @ /"}))
    cert = tmp_path / "root.pem"
    cert.write_text("test trust bundle")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_SECRET_FILE", str(secret))
    monkeypatch.setenv("DATABASE_SSL_ROOT_CERT", str(cert))
    monkeypatch.setenv("DATABASE_HOST", "test.us-east-1.rds.amazonaws.com")
    monkeypatch.setenv("DATABASE_NAME", "weather")
    result = conninfo_to_dict(database_dsn())
    assert result["sslmode"] == "verify-full"
    assert result["password"] == "space ' quote : @ /"
    assert result["host"] == "test.us-east-1.rds.amazonaws.com"
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/unexpected")
    with pytest.raises(ValueError, match="not both"):
        database_dsn()
    monkeypatch.delenv("DATABASE_URL")
    cert.unlink()
    with pytest.raises(ValueError, match="trust bundle"):
        database_dsn()


def test_runtime_initialization_never_runs_ddl(monkeypatch):
    assert conninfo_to_dict(db.DSN).get("dbname", "").endswith("_test"), "Use a disposable *_test database"
    db.migrate()
    monkeypatch.setenv("DATABASE_AUTO_MIGRATE", "0")
    monkeypatch.setattr(db, "migrate", lambda: pytest.fail("Runtime attempted DDL"))
    db.initialize()


def test_runtime_fails_closed_on_pending_migration(tmp_path, monkeypatch):
    assert conninfo_to_dict(db.DSN).get("dbname", "").endswith("_test"), "Use a disposable *_test database"
    db.migrate()
    (tmp_path / "999_future.sql").write_text("select 1;")
    monkeypatch.setattr(db, "MIGRATIONS", tmp_path)
    monkeypatch.setenv("DATABASE_AUTO_MIGRATE", "0")
    with pytest.raises(RuntimeError, match="pending"):
        db.initialize()


def test_runtime_invalid_migration_setting(monkeypatch):
    monkeypatch.setenv("DATABASE_AUTO_MIGRATE", "yes")
    with pytest.raises(ValueError):
        db.initialize()
