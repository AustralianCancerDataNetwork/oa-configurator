"""Shared fixtures for `oa-configurator` tests.

Rule: no test reads from or writes to ~/.config/omop/.
All tests use StackConfig.for_session() or tmp_path.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
import typer.rich_utils as _typer_rich_utils

from oa_configurator import (
    StackConfig,
    ConnectionConfig,
    CDMDatabaseConfig,
)
from oa_configurator.config import OAConfiguratorConfig
from oa_configurator.testing import DIALECT_PARAMS, isolated_test_database
from oa_configurator.domains.resources.sql import Dialect, Role

# typer forces colorized rich error/output rendering when GITHUB_ACTIONS (or
# FORCE_COLOR / PY_COLORS) is set -- see typer.rich_utils.FORCE_TERMINAL. Under
# GitHub Actions that injects ANSI escapes into CLI output, breaking tests that
# assert on plain-substring message content (e.g. "no such option: --foo"). The
# force feeds every typer rich Console, so clear it here: tests then see the same
# uncolored output everywhere; real users still get colour in a real terminal.
_typer_rich_utils.FORCE_TERMINAL = None

_FIELD_BY_DIALECT = {Dialect.POSTGRESQL: "test_db_pg", Dialect.SQLITE: "test_db_sqlite"}
assert set(_FIELD_BY_DIALECT) == {param.values[0] for param in DIALECT_PARAMS}, (
    "_FIELD_BY_DIALECT is missing an entry for a dialect DIALECT_PARAMS now covers."
)


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
        yield db.connection.engine.execution_options(schema_translate_map={Role.PRIMARY.value: "myschema"})


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
        if request.param == Dialect.SQLITE:
            other_path = db.connection.engine.url.database
            conn.execute(sa.text(f"ATTACH DATABASE '{other_path}_other' AS other_schema"))
            conn.execute(sa.text(f"CREATE TABLE other_schema.{table_name} (id INTEGER)"))
            conn.execute(
                sa.text(f"CREATE INDEX other_schema.{table_name}_idx ON {table_name} (id)")
            )
            conn.commit()
            yield conn, "other_schema", table_name
        elif request.param == Dialect.POSTGRESQL:
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
                dialect=Dialect.SQLITE,
                database_name=":memory:",
            )
        },
        databases={
            "default": CDMDatabaseConfig(connection="db"),
        },
    )


@pytest.fixture
def pg_stack() -> StackConfig:
    """In-memory config simulating a PostgreSQL CDM setup."""
    return StackConfig.for_session(
        connections={
            "cdm": ConnectionConfig(
                dialect=Dialect.POSTGRESQL+"+psycopg",
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
                cdm_schema="omop",
                vocab_schema="omop_vocab",
                results_schema="results",
            ),
        },
    )
