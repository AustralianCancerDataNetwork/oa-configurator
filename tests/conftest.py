"""Shared fixtures for `oa-configurator` tests.

Rule: no test reads from or writes to ~/.config/omop/.
All tests use StackConfig.for_session() or tmp_path.
"""

from __future__ import annotations


import pytest
import typer.rich_utils as _typer_rich_utils
from sqlalchemy.engine import make_url

from oa_configurator import (
    StackConfig,
    ConnectionConfig,
    CDMDatabaseConfig,
    ResolvedCDMDatabase,
    Resolver,
)
from oa_configurator.config import OAConfiguratorConfig
from oa_configurator.testing import DIALECT_PARAMS, isolated_test_database, reset_schema_registry_rows
from oa_configurator.domains.resources.sql import Dialect, Role, connection_key
from oa_configurator.domains.resources.schema_registry import (
    _ROLE_TAG_VALUES,
    SchemaRegistry,
    _row_conditions,
    _with_provenance_translate_map,
)
import sqlalchemy as sa

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


def registry_row(
    connection: sa.Connection, schema_tag: str, *, database_config_name: str | None = None
) -> sa.Row:
    """The schema_registry row for *schema_tag* on *connection*'s database.

    database_config_name is required for a Role tag (primary/vocab/results),
    since those rows are scoped per entry; omit it for a custom tag, whose
    rows are scoped connection-wide. Built on _row_conditions, the same
    predicate production code uses, so a test here can't drift from it.
    """
    if schema_tag in _ROLE_TAG_VALUES and database_config_name is None:
        raise ValueError(f"database_config_name is required for Role tag {schema_tag!r}.")
    mapped = _with_provenance_translate_map(connection, physical_schema="oa_configurator_provenance")
    conditions = _row_conditions(connection_key(connection.engine.url), schema_tag, database_config_name or "")
    return mapped.execute(sa.select(SchemaRegistry).where(*conditions)).one()


def register_entry(stack: StackConfig, database_config_name: str) -> None:
    """Register the entry's Role claims by building and disposing its engines."""
    resolved = Resolver(stack).resolve_database(database_config_name)
    engines = (
        resolved.create_engines()
        if isinstance(resolved, ResolvedCDMDatabase)
        else (resolved.create_engine(),)
    )
    for engine in set(engines):
        engine.dispose()


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


@pytest.fixture
def fresh_role_registry_rows(pg_db, cleanup_after_test):
    """Delete the Role-tag schema_registry rows on pg_db's database before and after the test.

    For tests that build non-test-only engines on the shared test database and
    need Role tags without a pre-existing baseline.
    """
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine)


@pytest.fixture
def pg_connection_config(pg_db) -> ConnectionConfig:
    """pg_db's database as a non-test_only ConnectionConfig, so engines built
    from it commit their registry writes and its guards fire.
    """
    url = make_url(pg_db.connection.engine.url)
    return ConnectionConfig(
        dialect=url.drivername, host=url.host, port=url.port,
        user=url.username, password=url.password, database_name=url.database,
    )


@pytest.fixture
def sqlite_db(request):
    """Isolated, disposable SQLite database, for a test that needs a real
    SQLite engine/connection with no Postgres counterpart (unlike the
    dialect-parametrized ``engine`` fixture below). ``test_db_sqlite`` is
    deliberately never configured, so this always falls back to
    ``SQLiteTestStrategy``'s own disposable database, a fresh tempfile with
    no rollback wrapping needed (SQLite isolation is free per-call).

    Use ``sqlite_db.connection`` where a ``Connection`` is needed,
    ``sqlite_db.committing_engine`` where a real ``Engine`` is needed (e.g.
    to open more than one connection/transaction in the same test).
    """
    with isolated_test_database(
        OAConfiguratorConfig, "test_db_sqlite", dialect=Dialect.SQLITE, request=request
    ) as db:
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

    Notes
    -----
    - Circumvents creat_engine()'s schema_claims as `primary` is a resolver-managed tag,
        so create_engine() rejects any schema_claims entry for it outright.
    - The schema_translate_map is set directly on the built engine instead, since
        this fixture wants an arbitrary, never-created schema name for testing
        sql.py's primitives.

    """
    with isolated_test_database(
        OAConfiguratorConfig, _FIELD_BY_DIALECT[request.param],
        dialect=request.param,
        request=request,
        schema_claims=()
    ) as db:
        yield db.connection.engine.execution_options(
            schema_translate_map={Role.PRIMARY.value: "myschema"},
        )


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
