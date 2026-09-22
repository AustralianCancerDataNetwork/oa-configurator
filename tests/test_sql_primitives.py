"""Cross-dialect tests for the schema-aware SQL primitives in
domains/resources/sql.py.

One shared test body runs against both sqlite (fully hermetic) and real
Postgres (via OA_Configurator's own test_db_pg field, see config.py)
for everything that's genuinely dialect-agnostic, via the parametrized
`engine` fixture below.

Only ensure_schema and guard_schema_provenance keep separate,
dialect-conditional test classes as the dialects genuinely behave
differently there (no-op vs real DDL; SQLite's supports_schemas()=False
collapses every schema_tag to the same None schema, which can't
meaningfully distinguish drift from a mere no-op).

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
from sqlalchemy.exc import InvalidRequestError

from oa_configurator import (
    ConnectionConfig,
    Resolver,
    Role,
    SchemaDriftError,
    StackConfig,
    autocommit_connection,
    ensure_schema,
    find_table_in_other_schemas,
    guard_schema_provenance,
    qualified,
    record_schema_provenance,
    register_reserved_schema,
    schema_of,
    supports_schemas,
    validate_schema_tag,
    Dialect,
)
from oa_configurator.domains.resources.sql import (
    _as_bind,
    _profile_for,
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

    def test_session_bound_to_an_engine_reduces_to_its_own_live_connection(self, engine):
        """Session.connection(), not Session.get_bind(): a fresh connection
        off the engine would be a second, independent connection, which on
        SQLite's SingletonThreadPool is the same underlying DBAPI connection
        the session itself is using, so closing it (as sa.inspect() does)
        would roll back the session's own uncommitted work."""
        session = so.Session(bind=engine)
        try:
            assert _as_bind(session) is session.connection()
        finally:
            session.close()


class TestValidateSchemaTag:
    def test_none_schema_returns_none(self):
        """Untagged is a legitimate, permanent case, not an error: schema_of()
        never redirects a None-schema table, it falls back to the connection's
        own default/search_path."""
        table = sa.Table("t", sa.MetaData(), schema=None)
        assert validate_schema_tag(table) is None

    def test_returns_a_known_role_value(self):
        table = sa.Table("t", sa.MetaData(), schema=Role.VOCAB.value)
        assert validate_schema_tag(table) == Role.VOCAB.value

    def test_returns_a_registered_reserved_schema(self):
        name = f"reserved_{uuid.uuid4().hex[:8]}"
        register_reserved_schema(name, owner="test-owner")
        table = sa.Table("t", sa.MetaData(), schema=name)
        assert validate_schema_tag(table) == name

    def test_raises_for_unrecognized_schema(self):
        table = sa.Table("t", sa.MetaData(), schema="extension")
        with pytest.raises(ValueError, match="extension"):
            validate_schema_tag(table)


class TestSchemaOf:
    def test_reads_the_default_schema_tags_key(self, engine):
        assert schema_of(engine) == "myschema"

    def test_falls_back_to_the_schema_tag_itself_when_no_map_at_all(self, engine):
        """No schema_translate_map on the bind at all: the schema_tag
        (Role.PRIMARY by default) is returned as-is, the same treatment a
        bare string gets."""
        bare = engine.execution_options(schema_translate_map=None)
        assert schema_of(bare) == Role.PRIMARY

    def test_works_through_a_session(self, engine):
        with engine.connect() as conn:
            session = so.Session(bind=conn)
            try:
                assert schema_of(session) == "myschema"
            finally:
                session.close()

    def test_schema_tag_reads_its_own_key_not_primary(self, engine):
        multi_tag = engine.execution_options(
            schema_translate_map={
                Role.PRIMARY.value: "myschema",
                Role.VOCAB.value: "vocabschema",
                Role.RESULTS.value: "resultsschema",
            }
        )
        assert schema_of(multi_tag, schema_tag=Role.VOCAB) == "vocabschema"
        assert schema_of(multi_tag, schema_tag=Role.RESULTS) == "resultsschema"
        assert schema_of(multi_tag) == "myschema"

    def test_none_schema_tag_short_circuits_without_consulting_the_map(self, engine):
        assert schema_of(engine, schema_tag=None) is None

    def test_bare_string_schema_tag_reads_its_own_mapped_key(self, engine):
        with_extension = engine.execution_options(
            schema_translate_map={Role.PRIMARY.value: "myschema", "extension": "ext_schema"}
        )
        assert schema_of(with_extension, schema_tag="extension") == "ext_schema"

    def test_bare_string_schema_tag_falls_back_to_itself_when_unmapped(self, engine):
        """Matches how SQLAlchemy's own schema_translate_map already treats an
        unmapped schema: untranslated, used as declared. Unlike a Role member,
        a bare string is itself a plausible literal schema name."""
        assert schema_of(engine, schema_tag="custom_schema") == "custom_schema"

    def test_bare_string_schema_tag_falls_back_to_itself_with_no_map_at_all(self, engine):
        bare = engine.execution_options(schema_translate_map=None)
        assert schema_of(bare, schema_tag="custom_schema") == "custom_schema"

    def test_role_member_unmapped_falls_back_to_its_own_value(self, engine):
        """A map is present but has no key for this schema_tag at all: falls
        back to the schema_tag itself, same as a bare string would (Role is
        a StrEnum). Unreached by any real ResolvedCDMDatabase-built map,
        which always writes all three Role keys; this only fires for a
        caller asking a split of an engine that was never built with one."""
        primary_only = engine.execution_options(
            schema_translate_map={Role.PRIMARY.value: "myschema"}
        )
        assert schema_of(primary_only, schema_tag=Role.VOCAB) == Role.VOCAB


class TestQualified:
    """Purely formatting: physical_schema must already be resolved by the
    caller (no default, no inference from a bind's schema_translate_map)."""

    def test_prefixes_with_the_given_physical_schema(self, engine):
        prep = engine.dialect.identifier_preparer
        assert qualified(engine, "concept", physical_schema="myschema") == (
            f"{prep.quote('myschema')}.{prep.quote('concept')}"
        )

    def test_none_produces_an_unqualified_name(self, engine):
        prep = engine.dialect.identifier_preparer
        assert qualified(engine, "concept", physical_schema=None) == prep.quote("concept")

    def test_quotes_mixed_case_identifiers(self, engine):
        assert qualified(engine, "MyTable", physical_schema=None) == '"MyTable"'

    def test_quotes_mixed_case_schema(self, engine):
        assert qualified(engine, "concept", physical_schema="MySchema") == '"MySchema".concept'


class TestSupportsSchemas:
    def test_sqlite_does_not(self):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect=Dialect.SQLITE, database_name=":memory:")}
        )
        eng = Resolver(cfg).resolve_connection("db").create_engine()
        try:
            assert supports_schemas(eng) is False
        finally:
            eng.dispose()

    def test_postgres_does(self, pg_db):
        """Used pg_db fixture rather than the parametrized engine fixture to prevent skipping this test"""
        assert supports_schemas(pg_db.connection) is True

    def test_accepts_a_dialect_name_string_directly(self):
        """A caller with only a ResolvedConnection/URL in hand shouldn't
        need to build an engine just to ask this."""
        assert supports_schemas(Dialect.SQLITE) is False
        assert supports_schemas(Dialect.POSTGRESQL) is True

    def test_unregistered_dialect_raises(self):
        """Only dialects this codebase actually models are supported --
        an unrecognized one raises rather than silently guessing."""
        with pytest.raises(ValueError, match="Unsupported dialect 'mysql'"):
            supports_schemas("mysql")


class TestAutocommitConnection:
    def test_from_an_engine(self, engine):
        with autocommit_connection(engine) as conn:
            assert conn.get_execution_options()["isolation_level"] == "AUTOCOMMIT"
            conn.execute(sa.text("SELECT 1"))  # runs with no explicit transaction/commit
        assert conn.closed

    def test_from_an_already_open_connection(self, engine):
        """Must be a fresh Connection with no transaction started yet:
        SQLAlchemy refuses to change isolation_level once a transaction is
        underway."""
        conn = engine.connect()
        try:
            previous_isolation_level = conn.get_isolation_level()
            with autocommit_connection(conn) as result:
                assert result is conn
                assert result.get_execution_options()["isolation_level"] == "AUTOCOMMIT"
                result.execute(sa.text("SELECT 1"))
            # isolation_level is restored, not left mutated, once the block exits.
            assert conn.get_isolation_level() == previous_isolation_level
        finally:
            conn.close()

    def test_restores_an_explicitly_set_isolation_level_not_just_the_driver_default(self, engine):
        conn = engine.connect()
        try:
            conn = conn.execution_options(isolation_level="SERIALIZABLE")
            with autocommit_connection(conn) as result:
                assert result.get_execution_options()["isolation_level"] == "AUTOCOMMIT"
            assert conn.get_execution_options()["isolation_level"] == "SERIALIZABLE"
        finally:
            conn.close()

    def test_refuses_a_connection_already_in_a_transaction(self, engine):
        conn = engine.connect()
        try:
            conn.begin()
            with pytest.raises(InvalidRequestError, match="active transaction"):
                with autocommit_connection(conn):
                    pass
        finally:
            conn.close()


class TestEnsureSchemaSqlite:
    """sqlite has no schema DDL at all. Every call is a no-op, regardless
    of *why* (arbitrary name, None, or "public"), covering the two distinct
    early-return branches ensure_schema has. The supports_schemas() branch
    short-circuits before the live default_schema_name lookup, so none of
    these touch a connection to decide."""

    @pytest.fixture
    def sqlite_engine(self):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect=Dialect.SQLITE, database_name=":memory:")}
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

    def test_noop_for_the_connections_live_default_schema(self, pg_db):
        """The no-op check now reads sa.inspect(bind).default_schema_name
        (live) rather than a static per-dialect guess -- confirm it still
        correctly no-ops for this connection's own real default ("public"
        for an unmodified search_path), not just skip the DDL by luck."""
        conn = pg_db.connection
        default = sa.inspect(conn).default_schema_name
        before = sa.inspect(conn).get_schema_names()
        ensure_schema(conn, default)
        assert sa.inspect(conn).get_schema_names() == before


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
        schemas = _profile_for(Dialect.POSTGRESQL).system_schemas
        assert "information_schema" in schemas
        assert "pg_catalog" in schemas

    def test_sqlite_has_none(self):
        assert _profile_for(Dialect.SQLITE).system_schemas == frozenset()

    def test_unregistered_dialect_raises(self):
        with pytest.raises(ValueError, match="Unsupported dialect 'not_a_real_dialect'"):
            _profile_for("not_a_real_dialect")


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
    SQLite's supports_schemas()=False collapses every schema_tag to the
    same None schema, which can't meaningfully distinguish these cases.

    guard_schema_provenance() takes plain database_name/test_only/
    schema_tag/physical_schema/tables directly, not a resolved config
    object -- these tests build no ResolvedDatabase at all.
    """

    def test_fresh_empty_schema_proceeds_and_records(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        with guard_schema_provenance(
            conn, database_name=db_name, test_only=False,
            schema_tag=Role.PRIMARY, physical_schema=schema, tables=(),
        ):
            ensure_schema(conn, schema)
            conn.execute(sa.text(f'CREATE TABLE "{schema}".t (id int)'))

    def test_agreeing_second_call_proceeds(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        with guard_schema_provenance(
            conn, database_name=db_name, test_only=False,
            schema_tag=Role.PRIMARY, physical_schema=schema, tables=(),
        ):
            ensure_schema(conn, schema)
        with guard_schema_provenance(
            conn, database_name=db_name, test_only=False,
            schema_tag=Role.PRIMARY, physical_schema=schema, tables=(),
        ):
            pass  # must not raise: same resolved schema as before

    def test_disagreeing_second_call_raises_schema_drift(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema_a = f"test_{uuid.uuid4().hex[:8]}"
        schema_b = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        with guard_schema_provenance(
            conn, database_name=db_name, test_only=False,
            schema_tag=Role.PRIMARY, physical_schema=schema_a, tables=(),
        ):
            ensure_schema(conn, schema_a)
        with pytest.raises(SchemaDriftError, match=f"{schema_a!r}.*{schema_b!r}"):
            with guard_schema_provenance(
                conn, database_name=db_name, test_only=False,
                schema_tag=Role.PRIMARY, physical_schema=schema_b, tables=(),
            ):
                ensure_schema(conn, schema_b)

    def test_test_only_short_circuits_even_on_drift(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema_a = f"test_{uuid.uuid4().hex[:8]}"
        schema_b = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        with guard_schema_provenance(
            conn, database_name=db_name, test_only=False,
            schema_tag=Role.PRIMARY, physical_schema=schema_a, tables=(),
        ):
            ensure_schema(conn, schema_a)
        with guard_schema_provenance(
            conn, database_name=db_name, test_only=True,
            schema_tag=Role.PRIMARY, physical_schema=schema_b, tables=(),
        ):
            pass  # must not raise despite disagreeing with the recorded schema

    def test_no_row_but_schema_already_populated_hard_stops(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        ensure_schema(conn, schema)
        conn.execute(sa.text(f'CREATE TABLE "{schema}".preexisting (id int)'))
        with pytest.raises(SchemaDriftError, match="no schema-provenance record"):
            with guard_schema_provenance(
                conn, database_name=db_name, test_only=False,
                schema_tag=Role.PRIMARY, physical_schema=schema, tables=(),
            ):
                pass

    def test_exception_in_body_does_not_record(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        with pytest.raises(ValueError, match="boom"):
            with guard_schema_provenance(
                conn, database_name=db_name, test_only=False,
                schema_tag=Role.PRIMARY, physical_schema=schema, tables=(),
            ):
                ensure_schema(conn, schema)
                raise ValueError("boom")
        # No record was written, so an empty schema now looks like day one again:
        # the guard would proceed silently rather than treat it as a stale claim.
        with guard_schema_provenance(
            conn, database_name=db_name, test_only=False,
            schema_tag=Role.PRIMARY, physical_schema=schema, tables=(),
        ):
            pass

    def test_table_present_under_a_different_schema_raises(self, pg_db):
        """The misconfiguration case: a table the caller is about to guard
        already physically exists under some other schema than
        physical_schema resolves to -- e.g. pre-existing vocab tables
        sitting in "myvocab" while vocab_schema is configured as "vocab".
        No provenance row exists yet, so the only signal is the table's
        real location."""
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        configured_schema = f"test_{uuid.uuid4().hex[:8]}"
        actual_schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        ensure_schema(conn, configured_schema)
        ensure_schema(conn, actual_schema)
        conn.execute(sa.text(f'CREATE TABLE "{actual_schema}".vocab_table (id int)'))
        table = sa.Table("vocab_table", sa.MetaData())
        with pytest.raises(SchemaDriftError, match=f"'vocab_table'.*{actual_schema!r}"):
            with guard_schema_provenance(
                conn, database_name=db_name, test_only=False,
                schema_tag=Role.VOCAB, physical_schema=configured_schema, tables=(table,),
            ):
                pass

    def test_table_absent_everywhere_does_not_false_positive(self, pg_db):
        """A table that doesn't yet exist anywhere on the connection (the
        common case: it's about to be created) must not be mistaken for a
        misplaced one. Note the target schema must stay empty here too --
        any pre-existing table under it, anywhere, trips the separate
        already_populated occupancy check first."""
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        ensure_schema(conn, schema)
        table = sa.Table("brand_new_table", sa.MetaData())
        with guard_schema_provenance(
            conn, database_name=db_name, test_only=False,
            schema_tag=Role.VOCAB, physical_schema=schema, tables=(table,),
        ):
            conn.execute(sa.text(f'CREATE TABLE "{schema}".brand_new_table (id int)'))


class TestGuardSchemaProvenanceSqlite:
    """Regression coverage for the bug this fix targets: on a fresh,
    non-test_only SQLite database with physical_schema=None,
    guard_schema_provenance used to create its own bookkeeping table
    before checking occupancy, then see that same just-created table and
    raise against its own bootstrap. SQLite's supports_schemas()=False
    collapses bookkeeping_schema and the guarded schema into the same flat
    None namespace, which is exactly what makes this reachable; every
    other guard_schema_provenance test in this file runs against real
    Postgres and can't exercise this path.
    """

    @pytest.fixture
    def sqlite_engine(self):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect=Dialect.SQLITE, database_name=":memory:")}
        )
        eng = Resolver(cfg).resolve_connection("db").create_engine()
        try:
            yield eng
        finally:
            eng.dispose()

    def test_fresh_bootstrap_does_not_false_positive_on_itself(self, sqlite_engine):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        with sqlite_engine.begin() as connection:
            with guard_schema_provenance(
                connection, database_name=db_name, test_only=False,
                schema_tag=Role.PRIMARY, physical_schema=None, tables=(),
            ):
                connection.execute(sa.text("CREATE TABLE t (id int)"))

    def test_genuinely_pre_populated_schema_still_raises(self, sqlite_engine):
        """The fix must not remove real drift detection, only the false
        positive against the guard's own bookkeeping table."""
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        with sqlite_engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE preexisting (id int)"))
        with sqlite_engine.begin() as connection:
            with pytest.raises(SchemaDriftError, match="no schema-provenance record"):
                with guard_schema_provenance(
                    connection, database_name=db_name, test_only=False,
                    schema_tag=Role.PRIMARY, physical_schema=None, tables=(),
                ):
                    pass

    def test_agreeing_second_call_proceeds(self, sqlite_engine):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        with sqlite_engine.begin() as connection:
            with guard_schema_provenance(
                connection, database_name=db_name, test_only=False,
                schema_tag=Role.PRIMARY, physical_schema=None, tables=(),
            ):
                connection.execute(sa.text("CREATE TABLE t (id int)"))
        with sqlite_engine.begin() as connection:
            with guard_schema_provenance(
                connection, database_name=db_name, test_only=False,
                schema_tag=Role.PRIMARY, physical_schema=None, tables=(),
            ):
                pass  # must not raise: same resolved schema as before


class TestRecordSchemaProvenance:
    def test_blank_reason_raises(self, pg_db):
        db_name = f"ack_{uuid.uuid4().hex[:8]}"
        with pytest.raises(ValueError, match="reason"):
            record_schema_provenance(
                pg_db.connection, database_name=db_name, schema_tag=Role.PRIMARY,
                new_physical_schema="s", reason="  ",
            )

    def test_recording_resolves_prior_drift(self, pg_db):
        """After recording a new baseline, the guard must accept it without raising."""
        db_name = f"ack_{uuid.uuid4().hex[:8]}"
        schema_a = f"test_{uuid.uuid4().hex[:8]}"
        schema_b = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        with guard_schema_provenance(
            conn, database_name=db_name, test_only=False,
            schema_tag=Role.PRIMARY, physical_schema=schema_a, tables=(),
        ):
            ensure_schema(conn, schema_a)

        record_schema_provenance(
            conn, database_name=db_name, schema_tag=Role.PRIMARY,
            new_physical_schema=schema_b, reason="deliberate migration in a test",
        )

        with guard_schema_provenance(
            conn, database_name=db_name, test_only=False,
            schema_tag=Role.PRIMARY, physical_schema=schema_b, tables=(),
        ):
            pass  # must not raise: recorded as the new baseline

    def test_recording_establishes_a_baseline_with_no_prior_row(self, pg_db):
        """Retrofit case: no row exists, target schema already has tables."""
        db_name = f"ack_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        ensure_schema(conn, schema)
        conn.execute(sa.text(f'CREATE TABLE "{schema}".preexisting (id int)'))

        with pytest.raises(SchemaDriftError):
            with guard_schema_provenance(
                conn, database_name=db_name, test_only=False,
                schema_tag=Role.PRIMARY, physical_schema=schema, tables=(),
            ):
                pass

        record_schema_provenance(
            conn, database_name=db_name, schema_tag=Role.PRIMARY,
            new_physical_schema=schema, reason="retrofit baseline",
        )

        with guard_schema_provenance(
            conn, database_name=db_name, test_only=False,
            schema_tag=Role.PRIMARY, physical_schema=schema, tables=(),
        ):
            pass  # must not raise now
