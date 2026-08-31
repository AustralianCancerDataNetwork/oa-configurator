"""Cross-dialect tests for the schema-aware SQL primitives in
domains/resources/sql.py: _as_bind, schema_of, qualified, schema_inspect,
schema_options, ensure_schema, autocommit_connection,
register_reserved_schema, reject_reserved_schema.

One shared test body runs against both sqlite (fully hermetic) and real
Postgres (via OA_Configurator's own test_postgres_db field, see config.py)
for everything that's genuinely dialect-agnostic, via the parametrized
`engine`/`probe_table` fixtures below. Not create_mock_engine:
MockConnection.schema_for_object ignores schema_translate_map entirely,
which would make a test pass whether or not translation actually works.

Only ensure_schema keeps separate, dialect-conditional test classes.
Sqlite and Postgres genuinely behave differently there (no-op vs real DDL),
which is the one place these primitives branch on dialect at all.

Rule: no test reads from ~/.config/omop/ directly (see conftest.py).
Postgres access goes through isolated_test_database(OAConfiguratorConfig,
"test_postgres_db"), which resolves by field name and skips cleanly when
that field isn't configured, whatever database it's been pointed at.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
import sqlalchemy.orm as so

from oa_configurator import (
    ConnectionConfig,
    Resolver,
    SchemaBoundInspector,
    StackConfig,
    autocommit_connection,
    ensure_schema,
    qualified,
    register_reserved_schema,
    schema_inspect,
    schema_of,
    schema_options,
    supports_schemas,
)
from oa_configurator.config import OAConfiguratorConfig
from oa_configurator.domains.resources.sql import _as_bind, reject_reserved_schema
from oa_configurator.testing import isolated_test_database

DIALECTS = ["sqlite", "postgres"]


@pytest.fixture(params=DIALECTS)
def engine(request):
    """A real engine per dialect, execution_options carrying an arbitrary
    schema_translate_map. The schema doesn't need to really exist. These
    primitives never query it, only read/build the dict and quote names."""
    if request.param == "sqlite":
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=":memory:")}
        )
        eng = Resolver(cfg).resolve_connection("db").create_engine().execution_options(
            schema_translate_map={None: "myschema"}
        )
        try:
            yield eng
        finally:
            eng.dispose()
    else:
        with isolated_test_database(OAConfiguratorConfig, "test_postgres_db") as db:
            yield db.connection.engine.execution_options(
                schema_translate_map={None: "myschema"}
            )


@pytest.fixture(params=DIALECTS)
def probe_table(request, tmp_path: Path):
    """(connection, schema_name, table_name) with a real table + index in a
    genuinely non-default schema, per dialect. sqlite's ATTACH DATABASE and
    Postgres's real CREATE SCHEMA are different mechanisms, but the same
    observable contract: a table schema_inspect() can find and a bare
    sa.inspect() can't."""
    table_name = "probe"
    if request.param == "sqlite":
        cfg = StackConfig.for_session(
            connections={
                "db": ConnectionConfig(dialect="sqlite", database_name=str(tmp_path / "main.db"))
            }
        )
        engine = Resolver(cfg).resolve_connection("db").create_engine()
        try:
            other_path = tmp_path / "other.db"
            with engine.connect() as conn:
                conn.execute(sa.text(f"ATTACH DATABASE '{other_path}' AS other_schema"))
                conn.execute(sa.text(f"CREATE TABLE other_schema.{table_name} (id INTEGER)"))
                conn.execute(
                    sa.text(f"CREATE INDEX other_schema.{table_name}_idx ON {table_name} (id)")
                )
                conn.commit()
                yield conn, "other_schema", table_name
        finally:
            engine.dispose()
    else:
        with isolated_test_database(OAConfiguratorConfig, "test_postgres_db") as db:
            conn = db.connection
            schema = f"test_{uuid.uuid4().hex[:8]}"
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(sa.text(f'CREATE TABLE "{schema}"."{table_name}" (id INTEGER)'))
            conn.execute(
                sa.text(f'CREATE INDEX "{table_name}_idx" ON "{schema}"."{table_name}" (id)')
            )
            yield conn, schema, table_name


class TestAsBind:
    def test_engine_passes_through(self, engine):
        assert _as_bind(engine) is engine

    def test_connection_passes_through(self, engine):
        with engine.connect() as conn:
            assert _as_bind(conn) is conn

    def test_session_reduces_to_its_bind(self, engine):
        with engine.connect() as conn:
            session = so.Session(bind=conn)
            try:
                assert _as_bind(session) is conn
            finally:
                session.close()


class TestSchemaOf:
    def test_reads_the_none_key(self, engine):
        assert schema_of(engine) == "myschema"

    def test_none_when_unset(self, engine):
        bare = engine.execution_options(schema_translate_map=None)
        assert schema_of(bare) is None

    def test_works_through_a_session(self, engine):
        with engine.connect() as conn:
            session = so.Session(bind=conn)
            try:
                assert schema_of(session) == "myschema"
            finally:
                session.close()


class TestQualified:
    def test_defaults_schema_from_bind(self, engine):
        prep = engine.dialect.identifier_preparer
        assert qualified(engine, "concept") == (
            f"{prep.quote('myschema')}.{prep.quote('concept')}"
        )

    def test_explicit_none_forces_unqualified(self, engine):
        prep = engine.dialect.identifier_preparer
        assert qualified(engine, "concept", schema=None) == prep.quote("concept")

    def test_explicit_schema_overrides_bind_default(self, engine):
        prep = engine.dialect.identifier_preparer
        assert qualified(engine, "concept", schema="other") == (
            f"{prep.quote('other')}.{prep.quote('concept')}"
        )

    def test_quotes_mixed_case_identifiers(self, engine):
        assert qualified(engine, "MyTable", schema=None) == '"MyTable"'

    def test_quotes_mixed_case_schema(self, engine):
        assert qualified(engine, "concept", schema="MySchema") == '"MySchema".concept'


class TestSchemaOptions:
    def test_defaults_schema_from_bind(self, engine):
        assert schema_options(engine) == {"schema_translate_map": {None: "myschema"}}

    def test_explicit_override(self, engine):
        assert schema_options(engine, schema="other") == {
            "schema_translate_map": {None: "other"}
        }


class TestSchemaInspect:
    def test_sees_a_table_a_bare_inspector_misses(self, probe_table):
        """The exact bug this primitive exists to fix: a plain sa.inspect()
        call with no schema= only sees the connection's default schema,
        silently missing a table that lives in another one."""
        conn, schema, table_name = probe_table
        bare = sa.inspect(conn)
        assert bare.has_table(table_name) is False

        bound = schema_inspect(conn, schema=schema)
        assert isinstance(bound, SchemaBoundInspector)
        assert bound.has_table(table_name) is True
        columns = bound.get_columns(table_name)
        assert [c["name"] for c in columns] == ["id"]
        assert table_name in bound.get_table_names()
        indexes = bound.get_indexes(table_name)
        assert [i["name"] for i in indexes] == [f"{table_name}_idx"]

    def test_per_call_override_wins_over_default(self, probe_table):
        conn, schema, table_name = probe_table
        bound = schema_inspect(conn, schema="does_not_exist")
        assert bound.has_table(table_name) is False
        assert bound.has_table(table_name, schema=schema) is True

    def test_delegates_unwrapped_methods_to_the_real_inspector(self, probe_table):
        conn, schema, _table_name = probe_table
        bound = schema_inspect(conn, schema=schema)
        bound.clear_cache()  # no schema-aware wrapper exists for this, must not raise


class TestSupportsSchemas:
    def test_sqlite_does_not(self):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=":memory:")}
        )
        eng = Resolver(cfg).resolve_connection("db").create_engine()
        try:
            assert supports_schemas(eng) is False
        finally:
            eng.dispose()

    def test_postgres_does(self, engine):
        if engine.dialect.name != "postgresql":
            pytest.skip("postgres-only")
        assert supports_schemas(engine) is True


class TestAutocommitConnection:
    def test_from_an_engine(self, engine):
        conn = autocommit_connection(engine)
        try:
            assert conn.get_execution_options()["isolation_level"] == "AUTOCOMMIT"
            conn.execute(sa.text("SELECT 1"))  # runs with no explicit transaction/commit
        finally:
            conn.close()

    def test_from_an_already_open_connection(self, engine):
        """Must be a fresh Connection with no transaction started yet:
        SQLAlchemy refuses to change isolation_level once a transaction is
        underway."""
        conn = engine.connect()
        try:
            result = autocommit_connection(conn)
            assert result is conn
            assert result.get_execution_options()["isolation_level"] == "AUTOCOMMIT"
            result.execute(sa.text("SELECT 1"))
        finally:
            conn.close()


class TestEnsureSchemaSqlite:
    """sqlite has no schema DDL at all. Every call is a no-op, regardless
    of *why* (arbitrary name, None, or "public"), covering the two distinct
    early-return branches ensure_schema has."""

    @pytest.fixture
    def sqlite_engine(self):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=":memory:")}
        )
        eng = Resolver(cfg).resolve_connection("db").create_engine()
        try:
            yield eng
        finally:
            eng.dispose()

    def test_noop_regardless_of_schema_name(self, sqlite_engine):
        before = sa.inspect(sqlite_engine).get_schema_names()
        ensure_schema(sqlite_engine, "myschema")
        assert sa.inspect(sqlite_engine).get_schema_names() == before

    def test_noop_for_none(self, sqlite_engine):
        before = sa.inspect(sqlite_engine).get_schema_names()
        ensure_schema(sqlite_engine, None)
        assert sa.inspect(sqlite_engine).get_schema_names() == before

    def test_noop_for_public(self, sqlite_engine):
        before = sa.inspect(sqlite_engine).get_schema_names()
        ensure_schema(sqlite_engine, "public")
        assert sa.inspect(sqlite_engine).get_schema_names() == before


class TestEnsureSchemaPostgres:
    """Real CREATE SCHEMA DDL and transaction participation. The one
    ensure_schema() branch sqlite structurally can't reach."""

    @pytest.fixture
    def pg_db(self):
        with isolated_test_database(OAConfiguratorConfig, "test_postgres_db") as db:
            yield db

    def test_with_connection_participates_in_callers_transaction(self, pg_db):
        """Given a Connection, must not open a nested transaction of its
        own. A rollback on the caller's transaction has to take the new
        schema with it."""
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        savepoint = conn.begin_nested()
        ensure_schema(conn, schema)
        exists = conn.execute(
            sa.text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
            {"s": schema},
        ).scalar()
        assert exists == 1
        savepoint.rollback()

        exists_after = conn.execute(
            sa.text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
            {"s": schema},
        ).scalar()
        assert exists_after is None

    def test_is_idempotent(self, pg_db):
        schema = f"test_{uuid.uuid4().hex[:8]}"
        ensure_schema(pg_db.connection, schema)
        ensure_schema(pg_db.connection, schema)  # must not raise


class TestReservedSchemas:
    """register_reserved_schema/reject_reserved_schema share one module-level
    registry, so every test uses a unique name (uuid-suffixed) to avoid
    colliding with other tests or with real callers in the same process."""

    def _name(self) -> str:
        return f"reserved_{uuid.uuid4().hex[:8]}"

    def test_reject_passes_for_none(self):
        reject_reserved_schema(None)  # must not raise

    def test_reject_passes_for_unregistered_name(self):
        reject_reserved_schema(self._name())  # must not raise

    def test_register_then_reject_raises(self):
        name = self._name()
        register_reserved_schema(name, owner="test-owner")
        with pytest.raises(RuntimeError, match=f"{name!r}.*test-owner"):
            reject_reserved_schema(name)

    def test_same_owner_reregistration_is_a_noop(self):
        name = self._name()
        register_reserved_schema(name, owner="test-owner")
        register_reserved_schema(name, owner="test-owner")  # must not raise
        with pytest.raises(RuntimeError):
            reject_reserved_schema(name)

    def test_different_owner_registration_raises(self):
        name = self._name()
        register_reserved_schema(name, owner="first-owner")
        with pytest.raises(RuntimeError, match=f"{name!r}.*first-owner.*second-owner"):
            register_reserved_schema(name, owner="second-owner")
