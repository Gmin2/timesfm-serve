"""Choose a disposable database before any application module creates its pool."""

import os

import pytest
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict

if os.environ.get("DATABASE_SECRET_FILE"):
    raise pytest.UsageError("Unset DATABASE_SECRET_FILE before running tests; never use runtime credentials")

os.environ.setdefault("DATABASE_URL", "postgresql://tfm:tfm@localhost:5432/tfm_test")
try:
    database = conninfo_to_dict(os.environ["DATABASE_URL"]).get("dbname", "")
except ProgrammingError as error:
    raise pytest.UsageError("DATABASE_URL must be a valid disposable test database DSN") from error
if not database.endswith("_test"):
    raise pytest.UsageError("Tests require a disposable database whose name ends with _test")
