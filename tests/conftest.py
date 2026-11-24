import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dsn(monkeypatch):
    server = os.getenv("GEODATAOPS_TEST_DSN")
    if not server:
        if os.getenv("GEODATAOPS_REQUIRE_INTEGRATION") == "1":
            pytest.fail("GEODATAOPS_TEST_DSN is required for integration tests", pytrace=False)
        pytest.skip("Set GEODATAOPS_TEST_DSN to run PostgreSQL/PostGIS integration tests")

    try:
        admin = psycopg.connect(server, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.fail("Could not connect to the PostgreSQL integration server", pytrace=False)

    monkeypatch.chdir(ROOT)
    name = f"geodataops_test_{uuid4().hex}"
    with admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        try:
            yield make_conninfo(server, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
