"""Local DSNs or a private, mounted Secrets Manager payload; never log either."""

import json
import os
from pathlib import Path

from psycopg.conninfo import make_conninfo


def database_dsn():
    secret_file = os.environ.get("DATABASE_SECRET_FILE")
    if not secret_file:
        return os.environ.get("DATABASE_URL", "postgresql://tfm:tfm@localhost:5432/tfm")
    if os.environ.get("DATABASE_URL"):
        raise ValueError("Configure DATABASE_SECRET_FILE or DATABASE_URL, not both")
    secret = json.loads(Path(secret_file).read_text())
    for key in ("username", "password"):
        if not isinstance(secret.get(key), str) or not secret[key]:
            raise ValueError("Invalid database secret shape")
    root_cert = Path(os.environ["DATABASE_SSL_ROOT_CERT"])
    if not root_cert.is_file():
        raise ValueError("Database TLS trust bundle is missing")
    return make_conninfo(
        host=os.environ["DATABASE_HOST"], port="5432", dbname=os.environ["DATABASE_NAME"],
        user=secret["username"], password=secret["password"],
        sslmode="verify-full", sslrootcert=str(root_cert), connect_timeout="5",
    )
