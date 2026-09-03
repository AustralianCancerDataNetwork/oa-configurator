"""Cross-dialect tests for the schema-aware SQL primitives in
domains/resources/sql.py: _as_bind, schema_of, qualified, schema_inspect,
schema_options, ensure_schema, autocommit_connection,
register_reserved_schema, reject_reserved_schema.

One shared test body runs against both sqlite (fully hermetic) and real
Postgres (via OA_Configurator's own test_db_pg field, see config.py)
for everything that's genuinely dialect-agnostic, via the parametrized
`engine`/`probe_table` fixtures below. Not create_mock_engine:
MockConnection.schema_for_object ignores schema_translate_map entirely,
which would make a test pass whether or not translation actually works.

Only ensure_schema keeps separate, dialect-conditional test classes.
Sqlite and Postgres genuinely behave differently there (no-op vs real DDL),
which is the one place these primitives branch on dialect at all.

Rule: no test reads from ~/.config/omop/ directly (see conftest.py).
Postgres access goes through isolated_test_database(OAConfiguratorConfig,
"test_db_pg"), which resolves by field name and skips cleanly when
that field isn't configured, whatever database it's been pointed at.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
import sqlalchemy.orm as so

from oa_configurator import (
    ConnectionConfig,
    ResolvedCDMDatabase,
    ResolvedConnection,
    ResolvedDatabase,
    Resolver,
    Role,
    SchemaBoundInspector,
    SchemaDriftError,
    StackConfig,
    autocommit_connection,
    ensure_schema,
    find_table_in_other_schemas,
    guard_schema_provenance,
    qualified,
    record_schema_provenance,
    register_reserved_schema,
    schema_inspect,
    schema_of,
    schema_options,
    supports_schemas,
)
from oa_configurator.domains.resources.sql import (
    _as_bind,
    _system_schemas_for,
    reject_reserved_schema,
)



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


class TestSystemSchemasFor:
    def test_postgres_excludes_its_catalogs(self):
        schemas = _system_schemas_for("postgresql")
        assert "information_schema" in schemas
        assert "pg_catalog" in schemas

    def test_unknown_dialect_is_empty(self):
        assert _system_schemas_for("sqlite") == frozenset()
        assert _system_schemas_for("not_a_real_dialect") == frozenset()


class TestFindTableInOtherSchemas:
    def test_finds_a_table_relocated_to_another_schema(self, pg_db):
        conn = pg_db.connection
        expected = f"test_{uuid.uuid4().hex[:8]}"
        actual = f"test_{uuid.uuid4().hex[:8]}"
        ensure_schema(conn, expected)
        ensure_schema(conn, actual)
        conn.execute(sa.text(f'CREATE TABLE "{actual}".orphan (id int)'))
        found = find_table_in_other_schemas(conn, "orphan", expected_schema=expected)
        assert found == (actual,)

    def test_empty_when_table_only_exists_where_expected(self, pg_db):
        conn = pg_db.connection
        expected = f"test_{uuid.uuid4().hex[:8]}"
        ensure_schema(conn, expected)
        conn.execute(sa.text(f'CREATE TABLE "{expected}".present (id int)'))
        found = find_table_in_other_schemas(conn, "present", expected_schema=expected)
        assert found == ()

    def test_empty_when_table_does_not_exist_anywhere(self, pg_db):
        found = find_table_in_other_schemas(
            pg_db.connection, "nonexistent_table_xyz", expected_schema="public"
        )
        assert found == ()

    def test_excludes_postgres_system_schemas(self, pg_db):
        found = find_table_in_other_schemas(
            pg_db.connection, "pg_tables", expected_schema="public"
        )
        assert "information_schema" not in found
        assert "pg_catalog" not in found


class TestGuardSchemaProvenance:
    """Guard's core drift semantics, exercised against real Postgres --
    SQLite's supports_schemas()=False collapses every role to the same
    None schema, which can't meaningfully distinguish these cases.
    """

    def _resolved(self, pg_db, *, database_name: str, schema_name: str) -> ResolvedDatabase:
        url = pg_db.connection.engine.url
        connection = ResolvedConnection(
            name="test_guard", url=url.render_as_string(hide_password=False),
            safe_url=url.render_as_string(hide_password=True), _engine_url=url,
        )
        return ResolvedDatabase(name=database_name, connection=connection, schema_name=schema_name)

    def _resolved_cdm(self, pg_db, *, database_name: str, schema_name: str) -> ResolvedCDMDatabase:
        url = pg_db.connection.engine.url
        connection = ResolvedConnection(
            name="test_guard", url=url.render_as_string(hide_password=False),
            safe_url=url.render_as_string(hide_password=True), _engine_url=url,
        )
        return ResolvedCDMDatabase(
            name=database_name, connection=connection, schema_name=schema_name,
            vocab_connection=connection, vocab_schema=schema_name, results_schema=schema_name,
        )

    def test_results_role_uses_the_primary_connection(self, pg_db):
        """Role.RESULTS has no connection of its own -- connection_target()
        returns the primary connection for it, and the guard must accept
        that rather than needing special-case handling for the role."""
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        resolved = self._resolved_cdm(pg_db, database_name=db_name, schema_name=schema)
        conn = pg_db.connection
        with guard_schema_provenance(conn, resolved, role=Role.RESULTS):
            ensure_schema(conn, schema)

    def test_fresh_empty_schema_proceeds_and_records(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        resolved = self._resolved(pg_db, database_name=db_name, schema_name=schema)
        conn = pg_db.connection
        with guard_schema_provenance(conn, resolved, role=Role.PRIMARY):
            ensure_schema(conn, schema)
            conn.execute(sa.text(f'CREATE TABLE "{schema}".t (id int)'))

    def test_agreeing_second_call_proceeds(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        resolved = self._resolved(pg_db, database_name=db_name, schema_name=schema)
        conn = pg_db.connection
        with guard_schema_provenance(conn, resolved, role=Role.PRIMARY):
            ensure_schema(conn, schema)
        with guard_schema_provenance(conn, resolved, role=Role.PRIMARY):
            pass  # must not raise: same resolved schema as before

    def test_disagreeing_second_call_raises_schema_drift(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema_a = f"test_{uuid.uuid4().hex[:8]}"
        schema_b = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        with guard_schema_provenance(conn, self._resolved(pg_db, database_name=db_name, schema_name=schema_a), role=Role.PRIMARY):
            ensure_schema(conn, schema_a)
        with pytest.raises(SchemaDriftError, match=f"{schema_a!r}.*{schema_b!r}"):
            with guard_schema_provenance(conn, self._resolved(pg_db, database_name=db_name, schema_name=schema_b), role=Role.PRIMARY):
                ensure_schema(conn, schema_b)

    def test_test_only_short_circuits_even_on_drift(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema_a = f"test_{uuid.uuid4().hex[:8]}"
        schema_b = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        with guard_schema_provenance(conn, self._resolved(pg_db, database_name=db_name, schema_name=schema_a), role=Role.PRIMARY):
            ensure_schema(conn, schema_a)
        with guard_schema_provenance(
            conn, self._resolved(pg_db, database_name=db_name, schema_name=schema_b),
            role=Role.PRIMARY, test_only=True,
        ):
            pass  # must not raise despite disagreeing with the recorded schema

    def test_no_row_but_schema_already_populated_hard_stops(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        ensure_schema(conn, schema)
        conn.execute(sa.text(f'CREATE TABLE "{schema}".preexisting (id int)'))
        with pytest.raises(SchemaDriftError, match="no schema-provenance record"):
            with guard_schema_provenance(conn, self._resolved(pg_db, database_name=db_name, schema_name=schema), role=Role.PRIMARY):
                pass

    def test_exception_in_body_does_not_record(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        resolved = self._resolved(pg_db, database_name=db_name, schema_name=schema)
        with pytest.raises(ValueError, match="boom"):
            with guard_schema_provenance(conn, resolved, role=Role.PRIMARY):
                ensure_schema(conn, schema)
                raise ValueError("boom")
        # No record was written, so an empty schema now looks like day one again:
        # the guard would proceed silently rather than treat it as a stale claim.
        with guard_schema_provenance(conn, resolved, role=Role.PRIMARY):
            pass


class TestRecordSchemaProvenance:
    def _resolved(self, pg_db, *, database_name: str, schema_name: str) -> ResolvedDatabase:
        url = pg_db.connection.engine.url
        connection = ResolvedConnection(
            name="ack_test", url=url.render_as_string(hide_password=False),
            safe_url=url.render_as_string(hide_password=True), _engine_url=url,
        )
        return ResolvedDatabase(name=database_name, connection=connection, schema_name=schema_name)

    def test_blank_reason_raises(self, pg_db):
        db_name = f"ack_{uuid.uuid4().hex[:8]}"
        resolved = self._resolved(pg_db, database_name=db_name, schema_name="whatever")
        with pytest.raises(ValueError, match="reason"):
            record_schema_provenance(pg_db.connection, resolved, role=Role.PRIMARY, new_schema="s", reason="  ")

    def test_recording_resolves_prior_drift(self, pg_db):
        """After recording a new baseline, the guard must accept it without raising."""
        db_name = f"ack_{uuid.uuid4().hex[:8]}"
        schema_a = f"test_{uuid.uuid4().hex[:8]}"
        schema_b = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        with guard_schema_provenance(conn, self._resolved(pg_db, database_name=db_name, schema_name=schema_a), role=Role.PRIMARY):
            ensure_schema(conn, schema_a)

        record_schema_provenance(
            conn, self._resolved(pg_db, database_name=db_name, schema_name=schema_b),
            role=Role.PRIMARY, new_schema=schema_b, reason="deliberate migration in a test",
        )

        with guard_schema_provenance(conn, self._resolved(pg_db, database_name=db_name, schema_name=schema_b), role=Role.PRIMARY):
            pass  # must not raise: recorded as the new baseline

    def test_recording_establishes_a_baseline_with_no_prior_row(self, pg_db):
        """Retrofit case: no row exists, target schema already has tables."""
        db_name = f"ack_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        ensure_schema(conn, schema)
        conn.execute(sa.text(f'CREATE TABLE "{schema}".preexisting (id int)'))
        resolved = self._resolved(pg_db, database_name=db_name, schema_name=schema)

        with pytest.raises(SchemaDriftError):
            with guard_schema_provenance(conn, resolved, role=Role.PRIMARY):
                pass

        record_schema_provenance(conn, resolved, role=Role.PRIMARY, new_schema=schema, reason="retrofit baseline")

        with guard_schema_provenance(conn, resolved, role=Role.PRIMARY):
            pass  # must not raise now
