"""Shared fixtures for `oa-configurator` tests.

Rule: no test reads from or writes to ~/.config/omop/.
All tests use StackConfig.for_session() or tmp_path.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from oa_configurator import (
    StackConfig,
    ConnectionConfig,
    CDMDatabaseConfig,
)
from oa_configurator.config import OAConfiguratorConfig
from oa_configurator.testing import DIALECT_PARAMS, isolated_test_database

_FIELD_BY_DIALECT = {"postgresql": "test_db_pg", "sqlite": "test_db_sqlite"}


@pytest.fixture
def pg_db(request):
    """Canonical isolated PostgreSQL test database. Resolves via
    OA_Configurator's own resource 'test_db_pg' in
    ~/.config/omop/config.toml. Everything a test does through
    pg_db.connection/pg_db.session happens inside one transaction that's
    rolled back on exit.
    """
    with isolated_test_database(OAConfiguratorConfig, "test_db_pg", request=request) as db:
        yield db


@pytest.fixture(params=DIALECT_PARAMS)
def engine(request):
    """A real engine per dialect, execution_options carrying an arbitrary
    schema_translate_map. The schema doesn't need to really exist. Shared
    across any test file exercising the schema-aware SQL primitives
    (domains/resources/sql.py), which never query it, only read/build the
    dict and quote names.

    dialect=request.param on isolated_test_database() resolves both cases
    through the one mechanism: test_db_sqlite is deliberately never
    configured, so that param falls back to SQLiteTestStrategy's disposable
    in-memory database automatically.
    """
    with isolated_test_database(
        OAConfiguratorConfig, _FIELD_BY_DIALECT[request.param], dialect=request.param, request=request
    ) as db:
        yield db.connection.engine.execution_options(schema_translate_map={None: "myschema"})


@pytest.fixture(params=DIALECT_PARAMS)
def probe_table(request):
    """(connection, schema_name, table_name) with a real table + index in a
    genuinely non-default schema, per dialect. sqlite's ATTACH DATABASE and
    Postgres's real CREATE SCHEMA are different mechanisms, but the same
    observable contract: a table schema_inspect() can find and a bare
    sa.inspect() can't."""
    table_name = "probe"
    with isolated_test_database(
        OAConfiguratorConfig, _FIELD_BY_DIALECT[request.param], dialect=request.param, request=request
    ) as db:
        conn = db.connection
        if request.param == "sqlite":
            other_path = db.connection.engine.url.database
            conn.execute(sa.text(f"ATTACH DATABASE '{other_path}_other' AS other_schema"))
            conn.execute(sa.text(f"CREATE TABLE other_schema.{table_name} (id INTEGER)"))
            conn.execute(
                sa.text(f"CREATE INDEX other_schema.{table_name}_idx ON {table_name} (id)")
            )
            conn.commit()
            yield conn, "other_schema", table_name
        elif request.param == "postgresql":
            schema = f"test_{uuid.uuid4().hex[:8]}"
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(sa.text(f'CREATE TABLE "{schema}"."{table_name}" (id INTEGER)'))
            conn.execute(
                sa.text(f'CREATE INDEX "{table_name}_idx" ON "{schema}"."{table_name}" (id)')
            )
            yield conn, schema, table_name
        else:
            raise ValueError(f"probe_table has no setup for dialect {request.param!r}.")


@pytest.fixture
def minimal_stack() -> StackConfig:
    """Minimal in-memory config with one SQLite connection and one database."""
    return StackConfig.for_session(
        connections={
            "db": ConnectionConfig(
                dialect="sqlite",
                database_name=":memory:",
            )
        },
        databases={
            "default": CDMDatabaseConfig(connection="db", schema_name="omop"),
        },
    )


@pytest.fixture
def pg_stack() -> StackConfig:
    """In-memory config simulating a PostgreSQL CDM setup."""
    return StackConfig.for_session(
        connections={
            "cdm": ConnectionConfig(
                dialect="postgresql+psycopg",
                host="localhost",
                port=5432,
                user="omop",
                password="secret",
                database_name="omop_cdm",
            )
        },
        databases={
            "default": CDMDatabaseConfig(
                connection="cdm",
                schema_name="omop",
                vocab_schema="omop_vocab",
                results_schema="results",
            ),
        },
    )
