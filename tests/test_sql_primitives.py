"""Cross-dialect tests for the schema-aware SQL primitives in
domains/resources/sql.py.

One shared test body runs against sqlite and real Postgres for
everything dialect-agnostic, via the parametrized `engine` fixture.

ensure_schema and _guard_schema_provenance keep separate,
dialect-conditional test classes: SQLite's supports_schemas()=False
collapses every schema_tag to the same None schema, which can't
distinguish drift from a no-op.

Rule: no test reads from ~/.config/omop/ directly (see conftest.py).
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
import sqlalchemy.orm as so
from sqlalchemy.exc import InvalidRequestError

from oa_configurator import (
    Role,
    SchemaDriftError,
    SchemaOwnershipError,
    UnregisteredSchemaTagError,
    autocommit_connection,
    ensure_schema,
    find_table_in_other_schemas,
    open_connection,
    qualified,
    physical_schema_of,
    supports_schemas,
    Dialect,
)
from oa_configurator.domains.resources.sql import _as_bind, _profile_for, connection_key
from oa_configurator.domains.resources.schema_registry import (
    SchemaRegistryOutdatedError,
    _guard_schema_provenance,
    _record_schema_provenance,
    _register_schema_claim,
    _reject_reservation_conflict,
)
from oa_configurator.testing import drop_schema_if_exists
from conftest import registry_row as _registry_row


class TestConnectionKey:
    def test_ignores_driver_and_credentials(self):
        a = sa.make_url("postgresql+psycopg://alice:pw@host:5432/omop")
        b = sa.make_url("postgresql://bob@host:5432/omop")
        assert connection_key(a) == connection_key(b)

    def test_distinguishes_databases(self):
        a = sa.make_url("postgresql://u@host:5432/omop")
        b = sa.make_url("postgresql://u@host:5432/other")
        assert connection_key(a) != connection_key(b)

    def test_collapses_a_hostname_and_its_own_ip(self):
        """The same server reached by hostname and by its IP must
        collide, since the registry it keys into lives inside the physical
        database and gains no real scoping from the spelling used to reach it."""
        a = sa.make_url("postgresql://u@localhost:5432/omop")
        b = sa.make_url("postgresql://u@127.0.0.1:5432/omop")
        assert connection_key(a) == connection_key(b)


class TestCanonicalHost:
    def test_resolves_localhost_to_its_loopback_ip(self):
        from oa_configurator.domains.resources.sql import canonical_host

        assert canonical_host("localhost") == "127.0.0.1"

    def test_none_passes_through(self):
        from oa_configurator.domains.resources.sql import canonical_host

        assert canonical_host(None) is None

    def test_an_unresolvable_host_is_returned_unchanged(self):
        """Soft-fails rather than raising: an unreachable/mock host (common
        in tests) still yields a usable, if un-canonicalized, value."""
        from oa_configurator.domains.resources.sql import canonical_host

        bogus = "this-host-does-not-resolve.invalid"
        assert canonical_host(bogus) == bogus


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


class TestOpenConnection:
    def test_engine_opens_its_own_transaction(self, engine):
        with open_connection(engine) as conn:
            assert conn.in_transaction()

    def test_connection_is_forwarded_as_is(self, engine):
        with engine.connect() as conn:
            with open_connection(conn) as opened:
                assert opened is conn

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


class TestPhysicalSchemaOf:
    """Every fallback/lookup case is folded through schema_if_supported: on
    a schema-less dialect (SQLite) the result is always None, regardless of
    what the map says or falls back to. A literal, unresolvable schema tag
    must never survive as a real schema name here.
    """

    def test_reads_the_default_schema_tags_key(self, engine):
        expected = "myschema" if supports_schemas(engine) else None
        assert physical_schema_of(engine) == expected

    def test_falls_back_to_the_schema_tag_itself_when_no_map_at_all(self, engine):
        """Checks that the a schema_tag not present in a schema_translate_map
        falls to the literal-fallback branch, which checks the schema_registry
        table. Since the schema_tag is arbitrary and unregistered, it raises
        on a dialect with a real schema concept, and is folded to None on SQLite."""

        bare = engine.execution_options(schema_translate_map=None)
        if supports_schemas(bare):
            with pytest.raises(UnregisteredSchemaTagError):
                physical_schema_of(bare)
        else:
            assert physical_schema_of(bare) is None

    def test_works_through_a_session(self, engine):
        expected = "myschema" if supports_schemas(engine) else None
        with engine.connect() as conn:
            session = so.Session(bind=conn)
            try:
                assert physical_schema_of(session) == expected
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
        if supports_schemas(multi_tag):
            assert physical_schema_of(multi_tag, schema_tag=Role.VOCAB) == "vocabschema"
            assert physical_schema_of(multi_tag, schema_tag=Role.RESULTS) == "resultsschema"
            assert physical_schema_of(multi_tag) == "myschema"
        else:
            assert physical_schema_of(multi_tag, schema_tag=Role.VOCAB) is None
            assert physical_schema_of(multi_tag, schema_tag=Role.RESULTS) is None
            assert physical_schema_of(multi_tag) is None

    def test_none_schema_tag_short_circuits_without_consulting_the_map(self, engine):
        assert physical_schema_of(engine, schema_tag=None) is None

    def test_bare_string_schema_tag_reads_its_own_mapped_key(self, engine):
        with_extension = engine.execution_options(
            schema_translate_map={Role.PRIMARY.value: "myschema", "extension": "ext_schema"}
        )
        expected = "ext_schema" if supports_schemas(with_extension) else None
        assert physical_schema_of(with_extension, schema_tag="extension") == expected

    def test_bare_string_schema_tag_falls_back_to_itself_when_unmapped(self, engine):
        """A bare string schema_tag is itself a plausible literal schema
        name,so it falls to the literal-fallback branch. 
        On a dialect with no schema concept it's folded to None;
        otherwise it's checked against the schema_registry, and this
        arbitrary, unregistered name raises."""
        if supports_schemas(engine):
            with pytest.raises(UnregisteredSchemaTagError):
                physical_schema_of(engine, schema_tag="custom_schema")
        else:
            assert physical_schema_of(engine, schema_tag="custom_schema") is None

    def test_bare_string_schema_tag_falls_back_to_itself_with_no_map_at_all(self, engine):
        bare = engine.execution_options(schema_translate_map=None)
        if supports_schemas(bare):
            with pytest.raises(UnregisteredSchemaTagError):
                physical_schema_of(bare, schema_tag="custom_schema")
        else:
            assert physical_schema_of(bare, schema_tag="custom_schema") is None

    def test_role_member_unmapped_falls_back_to_its_own_value(self, engine):
        """A map is present but has no key for this schema_tag: falls to the
        literal-fallback branch, same as a bare string (Role is a StrEnum).
        Folded to None on a dialect with no schema concept; otherwise
        checked against the schema_registry, where "vocab" was never
        registered. Unreached by any real ResolvedCDMDatabase-built map,
        which always writes all three Role keys."""
        primary_only = engine.execution_options(
            schema_translate_map={Role.PRIMARY.value: "myschema"}
        )
        if supports_schemas(primary_only):
            with pytest.raises(UnregisteredSchemaTagError):
                physical_schema_of(primary_only, schema_tag=Role.VOCAB)
        else:
            assert physical_schema_of(primary_only, schema_tag=Role.VOCAB) is None


class TestPhysicalSchemaOfUnregisteredTagValidation:
    """The literal-fallback branch checks the connection's own
    schema_registry table directly. Needs a real, live Postgres
    connection, unlike the rest of this file's dialect-parametrized
    ``engine`` fixture."""

    def test_unmapped_tag_not_registered_raises(self, pg_db):
        with pytest.raises(UnregisteredSchemaTagError, match="typo_tag"):
            physical_schema_of(pg_db.connection, schema_tag=f"typo_tag_{uuid.uuid4().hex[:8]}")

    def test_mapped_tag_never_consults_the_registry(self, pg_db):
        """A tag that resolves via schema_translate_map is never checked
        against the schema_registry; only the literal-fallback branch is."""
        mapped = pg_db.connection.execution_options(
            schema_translate_map={Role.PRIMARY.value: "myschema"}
        )
        assert physical_schema_of(mapped, schema_tag=Role.PRIMARY) == "myschema"


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

    def test_accepts_an_identifier_preparer_directly(self, engine):
        """A caller with no live bindable (e.g. a dialect-only preparer built
        ahead of any connection) can pass the preparer itself."""
        preparer = engine.dialect.identifier_preparer
        assert qualified(preparer, "concept", physical_schema="myschema") == qualified(
            engine, "concept", physical_schema="myschema"
        )


class TestSupportsSchemas:
    def test_sqlite_does_not(self, sqlite_db):
        assert supports_schemas(sqlite_db.committing_engine) is False

    def test_postgres_does(self, pg_db):
        """Used pg_db fixture rather than the parametrized engine fixture to prevent skipping this test"""
        assert supports_schemas(pg_db.connection) is True

    def test_accepts_a_dialect_name_string_directly(self):
        """A caller with only a ResolvedConnection/URL in hand shouldn't
        need to build an engine just to ask this."""
        assert supports_schemas(Dialect.SQLITE) is False
        assert supports_schemas(Dialect.POSTGRESQL) is True

    def test_unregistered_dialect_raises(self):
        """Only dialects this codebase models are supported; an
        unrecognized one raises rather than silently guessing."""
        with pytest.raises(ValueError, match="Unsupported dialect 'mysql'"):
            supports_schemas("mysql")


class TestAutocommitConnection:
    def test_from_an_engine(self, engine):
        with autocommit_connection(engine) as conn:
            assert conn.get_execution_options()["isolation_level"] == "AUTOCOMMIT"
            conn.execute(sa.text("SELECT 1"))  # runs with no explicit transaction/commit
        assert conn.closed

    def test_from_an_already_open_connection(self, engine):
        """Requires a fresh Connection with no transaction started, since
        isolation_level cannot change once a transaction is underway."""
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

    def test_noop_regardless_of_schema_name(self, sqlite_db):
        engine = sqlite_db.committing_engine
        before = sa.inspect(engine).get_schema_names()
        ensure_schema(engine, "myschema")
        assert sa.inspect(engine).get_schema_names() == before

    def test_noop_for_none(self, sqlite_db):
        engine = sqlite_db.committing_engine
        before = sa.inspect(engine).get_schema_names()
        ensure_schema(engine, None)
        assert sa.inspect(engine).get_schema_names() == before

    def test_noop_for_public(self, sqlite_db):
        engine = sqlite_db.committing_engine
        before = sa.inspect(engine).get_schema_names()
        ensure_schema(engine, "public")
        assert sa.inspect(engine).get_schema_names() == before


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
        """Confirms the no-op check for this connection's own live default
        schema ("public" for an unmodified search_path), not a
        coincidental skip."""
        conn = pg_db.connection
        default = sa.inspect(conn).default_schema_name
        before = sa.inspect(conn).get_schema_names()
        ensure_schema(conn, default)
        assert sa.inspect(conn).get_schema_names() == before


class TestRegisterSchemaClaim:
    """Every check is scoped to one connection via connection.engine.url,
    so a disposable SQLite connection exercises the real logic; no live
    Postgres needed here."""

    def _name(self) -> str:
        return f"reserved_{uuid.uuid4().hex[:8]}"

    def test_same_owner_same_physical_schema_is_a_noop(self, sqlite_db):
        tag, name = self._name(), self._name()
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag=tag, physical_schema=name, owner="pkg",
        )
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag=tag, physical_schema=name, owner="pkg",
        )  # must not raise

    def test_different_owner_same_tag_different_schema_raises(self, sqlite_db):
        tag = self._name()
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag=tag,
            physical_schema=self._name(), owner="first-owner",
        )
        with pytest.raises(SchemaOwnershipError, match=f"{tag!r}.*first-owner"):
            _register_schema_claim(
                sqlite_db.connection, database_config_name="db2", schema_tag=tag,
                physical_schema=self._name(), owner="second-owner",
            )

    def test_different_owner_same_tag_same_schema_raises(self, sqlite_db):
        """A second owner must not silently inherit ownership of a tag just
        because its physical schema happens to coincide with the first
        owner's: that would be an implicit, undetected ownership transfer.
        Ownership transfer must go through release-schema-claim."""
        tag, schema = self._name(), self._name()
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag=tag,
            physical_schema=schema, owner="first-owner",
        )
        with pytest.raises(SchemaOwnershipError, match=f"{tag!r}.*first-owner"):
            _register_schema_claim(
                sqlite_db.connection, database_config_name="db2", schema_tag=tag,
                physical_schema=schema, owner="second-owner",
            )

    def test_different_tags_sharing_one_physical_schema_is_fine(self, sqlite_db):
        """Deliberately allowed: results_schema falling back to cdm_schema
        does exactly this in real use."""
        shared = self._name()
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag="primary", physical_schema=shared,
        )
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag="results", physical_schema=shared,
        )  # must not raise

    def test_none_physical_schema_is_a_noop(self, sqlite_db):
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag="primary", physical_schema=None,
        )  # must not raise, and writes nothing


class TestRegisterSchemaClaimAlreadyPopulated:
    """Runs at create_engine() time, not deferred until DDL runs. Needs a
    real multi-schema dialect: physical_schema is an arbitrary label on
    SQLite, not a real queryable namespace."""

    def _name(self) -> str:
        return f"reserved_{uuid.uuid4().hex[:8]}"

    def test_first_registration_of_a_role_tag_against_pre_existing_tables_raises(self, pg_db):
        schema = self._name()
        conn = pg_db.connection
        ensure_schema(conn, schema)
        conn.execute(sa.text(f'CREATE TABLE "{schema}".preexisting (id int)'))
        with pytest.raises(SchemaDriftError, match="no schema-registry record"):
            _register_schema_claim(
                conn, database_config_name="db1", schema_tag="primary", physical_schema=schema,
            )

    def test_first_registration_of_a_custom_tag_against_pre_existing_tables_proceeds(self, pg_db):
        """Unlike a Role tag, a custom tag is declared by the owning
        package's own code. It registers directly since the config has 
        no visibility into what its baseline should be."""
        schema = self._name()
        conn = pg_db.connection
        ensure_schema(conn, schema)
        conn.execute(sa.text(f'CREATE TABLE "{schema}".preexisting (id int)'))
        _register_schema_claim(
            conn, database_config_name="db1", schema_tag=self._name(), physical_schema=schema,
        )  # must not raise

    def test_first_registration_against_an_empty_schema_proceeds(self, pg_db):
        schema = self._name()
        _register_schema_claim(
            pg_db.connection, database_config_name="db1", schema_tag=self._name(), physical_schema=schema,
        )  # must not raise


class TestReservation:
    """_register_schema_claim(reserved=True)/_reject_reservation_conflict
    guard a physical schema no other owner may use on the same connection,
    independent of any schema_tag."""

    def _name(self) -> str:
        return f"reserved_{uuid.uuid4().hex[:8]}"

    def test_reject_passes_for_none(self, sqlite_db):
        _reject_reservation_conflict(sqlite_db.connection, physical_schema=None)  # must not raise

    def test_reject_passes_for_unreserved_name(self, sqlite_db):
        _reject_reservation_conflict(sqlite_db.connection, physical_schema=self._name())  # must not raise

    def test_reserve_then_reject_raises(self, sqlite_db):
        name = self._name()
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag=name,
            physical_schema=name, owner="test-owner", reserved=True,
        )
        with pytest.raises(SchemaOwnershipError, match=f"{name!r}.*test-owner"):
            _reject_reservation_conflict(sqlite_db.connection, physical_schema=name)

    def test_same_owner_rereservation_is_a_noop(self, sqlite_db):
        name = self._name()
        for _ in range(2):
            _register_schema_claim(
                sqlite_db.connection, database_config_name="db1", schema_tag=name,
                physical_schema=name, owner="test-owner", reserved=True,
            )  # must not raise
        with pytest.raises(SchemaOwnershipError):
            _reject_reservation_conflict(sqlite_db.connection, physical_schema=name)

    def test_different_owner_reservation_raises(self, sqlite_db):
        name = self._name()
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag=name,
            physical_schema=name, owner="first-owner", reserved=True,
        )
        with pytest.raises(SchemaOwnershipError, match=f"{name!r}.*first-owner"):
            _register_schema_claim(
                sqlite_db.connection, database_config_name="db2", schema_tag=name,
                physical_schema=name, owner="second-owner", reserved=True,
            )

    def test_reserving_a_schema_another_owner_uses_raises(self, sqlite_db):
        name = self._name()
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag=self._name(),
            physical_schema=name, owner="first-owner",
        )
        with pytest.raises(SchemaOwnershipError, match=f"{name!r}.*already used by.*'first-owner'"):
            _register_schema_claim(
                sqlite_db.connection, database_config_name="db2", schema_tag=self._name(),
                physical_schema=name, owner="second-owner", reserved=True,
            )

    def test_reserving_a_schema_the_same_owner_uses_is_fine(self, sqlite_db):
        name = self._name()
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag=self._name(),
            physical_schema=name, owner="test-owner",
        )
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag=self._name(),
            physical_schema=name, owner="test-owner", reserved=True,
        )  # must not raise

    def test_an_ownerless_reservation_is_enforced(self, sqlite_db):
        name = self._name()
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag=self._name(),
            physical_schema=name, reserved=True,
        )
        with pytest.raises(SchemaOwnershipError, match=f"{name!r}.*already reserved"):
            _register_schema_claim(
                sqlite_db.connection, database_config_name="db2", schema_tag=self._name(),
                physical_schema=name,
            )

    def test_reasserting_unreserved_releases_the_reservation(self, sqlite_db):
        name = self._name()
        for reserved in (True, False):
            _register_schema_claim(
                sqlite_db.connection, database_config_name="db1", schema_tag=name,
                physical_schema=name, owner="first-owner", reserved=reserved,
            )
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db2", schema_tag=self._name(),
            physical_schema=name, owner="second-owner",
        )  # must not raise

    def test_reasserting_as_reserved_rejects_another_owners_use(self, sqlite_db):
        name = self._name()
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag=name,
            physical_schema=name, owner="first-owner",
        )
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db2", schema_tag=self._name(),
            physical_schema=name, owner="second-owner",
        )
        with pytest.raises(SchemaOwnershipError, match=f"{name!r}.*already used by.*'second-owner'"):
            _register_schema_claim(
                sqlite_db.connection, database_config_name="db1", schema_tag=name,
                physical_schema=name, owner="first-owner", reserved=True,
            )

    def test_reserving_a_physical_schema_does_not_block_it_as_a_tag_claim(self, sqlite_db):
        """The two checks are independent: a reservation guards physical_schema
        equality; a normal tag claim is keyed by schema_tag, not by reuse of
        the same literal string as some other tag's physical schema."""
        name = self._name()
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag=name,
            physical_schema=name, owner="test-owner", reserved=True,
        )
        _register_schema_claim(
            sqlite_db.connection, database_config_name="db1", schema_tag="unrelated_tag",
            physical_schema=self._name(), owner="test-owner",
        )  # must not raise


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
        found = find_table_in_other_schemas(conn, "orphan", physical_schema=expected)
        assert found == (actual,)

    def test_empty_when_table_only_exists_where_expected(self, pg_db):
        conn = pg_db.connection
        expected = f"test_{uuid.uuid4().hex[:8]}"
        ensure_schema(conn, expected)
        conn.execute(sa.text(f'CREATE TABLE "{expected}".present (id int)'))
        found = find_table_in_other_schemas(conn, "present", physical_schema=expected)
        assert found == ()

    def test_empty_when_table_does_not_exist_anywhere(self, pg_db):
        found = find_table_in_other_schemas(
            pg_db.connection, "nonexistent_table_xyz", physical_schema="public"
        )
        assert found == ()

    def test_excludes_postgres_system_schemas(self, pg_db):
        found = find_table_in_other_schemas(
            pg_db.connection, "pg_tables", physical_schema="public"
        )
        assert "information_schema" not in found
        assert "pg_catalog" not in found


class TestGuardSchemaProvenance:
    """Guard's core drift semantics, exercised against real Postgres:
    SQLite's supports_schemas()=False collapses every schema_tag to the
    same None schema, which can't distinguish these cases.

    _guard_schema_provenance() is read-then-compare: the baseline row
    must already exist. These tests call _register_schema_claim()
    directly first, standing in for what create_engine() would have
    done, since none of them build a ResolvedDatabase.
    """

    def test_fresh_registration_then_guard_proceeds(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(
            conn, database_config_name=db_name, schema_tag=tag, physical_schema=schema,
        )
        with _guard_schema_provenance(
            conn, database_config_name=db_name, test_only=False,
            schema_tag=tag, physical_schema=schema,
        ):
            ensure_schema(conn, schema)
            conn.execute(sa.text(f'CREATE TABLE "{schema}".t (id int)'))

    def test_agreeing_second_call_proceeds(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(
            conn, database_config_name=db_name, schema_tag=tag, physical_schema=schema,
        )
        with _guard_schema_provenance(
            conn, database_config_name=db_name, test_only=False,
            schema_tag=tag, physical_schema=schema,
        ):
            pass
        with _guard_schema_provenance(
            conn, database_config_name=db_name, test_only=False,
            schema_tag=tag, physical_schema=schema,
        ):
            pass  # must not raise: same resolved schema as before

    def test_disagreeing_call_raises_schema_drift(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema_a = f"test_{uuid.uuid4().hex[:8]}"
        schema_b = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(
            conn, database_config_name=db_name, schema_tag=tag, physical_schema=schema_a,
        )
        with pytest.raises(SchemaDriftError, match=f"{schema_b!r}.*{schema_a!r}"):
            with _guard_schema_provenance(
                conn, database_config_name=db_name, test_only=False,
                schema_tag=tag, physical_schema=schema_b,
            ):
                pass

    def test_test_only_short_circuits_even_on_drift(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema_a = f"test_{uuid.uuid4().hex[:8]}"
        schema_b = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(
            conn, database_config_name=db_name, schema_tag=tag, physical_schema=schema_a,
        )
        with _guard_schema_provenance(
            conn, database_config_name=db_name, test_only=True,
            schema_tag=tag, physical_schema=schema_b,
        ):
            pass  # must not raise despite disagreeing with the recorded schema

    def test_no_baseline_registered_raises(self, pg_db):
        """_register_schema_claim()/create_engine() must run with this
        claim before it can be guarded; the guard itself never establishes
        a baseline, regardless of whether the schema is already
        populated."""
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        with pytest.raises(SchemaDriftError, match="No schema-registry baseline"):
            with _guard_schema_provenance(
                conn, database_config_name=db_name, test_only=False,
                schema_tag=tag, physical_schema=schema,
            ):
                pass

    def test_exception_in_body_does_not_disturb_the_baseline(self, pg_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(
            conn, database_config_name=db_name, schema_tag=tag, physical_schema=schema,
        )
        with pytest.raises(ValueError, match="boom"):
            with _guard_schema_provenance(
                conn, database_config_name=db_name, test_only=False,
                schema_tag=tag, physical_schema=schema,
            ):
                raise ValueError("boom")
        # Baseline is untouched by the raise; a second, clean call still succeeds.
        with _guard_schema_provenance(
            conn, database_config_name=db_name, test_only=False,
            schema_tag=tag, physical_schema=schema,
        ):
            pass

    def test_another_config_entry_shares_the_baseline(self, pg_db):
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(conn, database_config_name="entry_a", schema_tag=tag, physical_schema=schema)
        with _guard_schema_provenance(
            conn, database_config_name="entry_b", test_only=False,
            schema_tag=tag, physical_schema=schema,
        ):
            pass  # must not raise: the baseline belongs to the physical database

    def test_drift_message_names_both_config_entries(self, pg_db):
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema_a = f"test_{uuid.uuid4().hex[:8]}"
        schema_b = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(conn, database_config_name="entry_a", schema_tag=tag, physical_schema=schema_a)
        with pytest.raises(SchemaDriftError, match=f"'entry_b'.*{schema_b!r}.*'entry_a'.*{schema_a!r}"):
            with _guard_schema_provenance(
                conn, database_config_name="entry_b", test_only=False,
                schema_tag=tag, physical_schema=schema_b,
            ):
                pass

    def test_reasserting_a_claim_keeps_the_establishing_entry(self, pg_db):
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(conn, database_config_name="entry_a", schema_tag=tag, physical_schema=schema)
        _register_schema_claim(conn, database_config_name="entry_b", schema_tag=tag, physical_schema=schema)
        assert _registry_row(conn, tag).database_config_name == "entry_a"

    def test_a_differing_claim_raises_instead_of_silently_leaving_the_row_untouched(self, pg_db):
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema_a = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(conn, database_config_name="entry_a", schema_tag=tag, physical_schema=schema_a)
        with pytest.raises(SchemaDriftError, match="entry_a"):
            _register_schema_claim(
                conn, database_config_name="entry_b", schema_tag=tag,
                physical_schema=f"test_{uuid.uuid4().hex[:8]}",
            )
        row = _registry_row(conn, tag)
        assert (row.database_config_name, row.physical_schema) == ("entry_a", schema_a)


class TestGuardSchemaProvenanceSqlite:
    """physical_schema=None means this dialect has no schema concept at
    all (e.g. SQLite): nothing to protect, nothing to check, regardless
    of test_only or whether any baseline was ever registered."""

    def test_no_baseline_is_a_noop_not_a_hard_error(self, sqlite_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        with sqlite_db.committing_engine.begin() as connection:
            with _guard_schema_provenance(
                connection, database_config_name=db_name, test_only=False,
                schema_tag=Role.PRIMARY, physical_schema=None,
            ):
                connection.execute(sa.text("CREATE TABLE t (id int)"))

    def test_pre_populated_schema_does_not_raise_either(self, sqlite_db):
        db_name = f"guard_{uuid.uuid4().hex[:8]}"
        with sqlite_db.committing_engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE preexisting (id int)"))
        with sqlite_db.committing_engine.begin() as connection:
            with _guard_schema_provenance(
                connection, database_config_name=db_name, test_only=False,
                schema_tag=Role.PRIMARY, physical_schema=None,
            ):
                pass  # must not raise: nothing is tracked for this dialect at all


class TestRecordSchemaProvenance:
    def test_blank_reason_raises(self, pg_db):
        db_name = f"ack_{uuid.uuid4().hex[:8]}"
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        with pytest.raises(ValueError, match="reason"):
            _record_schema_provenance(
                pg_db.connection, database_config_name=db_name, schema_tag=tag,
                new_physical_schema="s", reason="  ",
            )

    def test_recording_resolves_prior_drift(self, pg_db):
        """After recording a new baseline, the guard must accept it without raising."""
        db_name = f"ack_{uuid.uuid4().hex[:8]}"
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema_a = f"test_{uuid.uuid4().hex[:8]}"
        schema_b = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(
            conn, database_config_name=db_name, schema_tag=tag, physical_schema=schema_a,
        )
        with _guard_schema_provenance(
            conn, database_config_name=db_name, test_only=False,
            schema_tag=tag, physical_schema=schema_a,
        ):
            pass

        _record_schema_provenance(
            conn, database_config_name=db_name, schema_tag=tag,
            new_physical_schema=schema_b, reason="deliberate migration in a test",
        )

        with _guard_schema_provenance(
            conn, database_config_name=db_name, test_only=False,
            schema_tag=tag, physical_schema=schema_b,
        ):
            pass  # must not raise: recorded as the new baseline

    def test_recording_establishes_a_baseline_with_no_prior_row(self, pg_db):
        """Retrofit case: no baseline was ever registered, so the guard
        has no row to compare against, regardless of whether the target
        schema already has tables."""
        db_name = f"ack_{uuid.uuid4().hex[:8]}"
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        ensure_schema(conn, schema)
        conn.execute(sa.text(f'CREATE TABLE "{schema}".preexisting (id int)'))

        with pytest.raises(SchemaDriftError):
            with _guard_schema_provenance(
                conn, database_config_name=db_name, test_only=False,
                schema_tag=tag, physical_schema=schema,
            ):
                pass

        _record_schema_provenance(
            conn, database_config_name=db_name, schema_tag=tag,
            new_physical_schema=schema, reason="retrofit baseline",
        )

        with _guard_schema_provenance(
            conn, database_config_name=db_name, test_only=False,
            schema_tag=tag, physical_schema=schema,
        ):
            pass  # must not raise now

    def test_recording_transfers_the_mapping_to_the_acknowledging_entry(self, pg_db):
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema_a = f"test_{uuid.uuid4().hex[:8]}"
        schema_b = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(conn, database_config_name="entry_a", schema_tag=tag, physical_schema=schema_a)
        _record_schema_provenance(
            conn, database_config_name="entry_b", schema_tag=tag,
            new_physical_schema=schema_b, reason="entry_b takes over",
        )
        row = _registry_row(conn, tag)
        assert (row.database_config_name, row.physical_schema, row.previous_physical_schema) == (
            "entry_b", schema_b, schema_a,
        )

    def test_reacknowledging_a_reserved_tag_onto_its_own_schema(self, pg_db):
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(
            conn, database_config_name="entry", schema_tag=tag, physical_schema=schema,
            owner="pkg", reserved=True,
        )
        for _ in range(2):
            _record_schema_provenance(
                conn, database_config_name="entry", schema_tag=tag,
                new_physical_schema=schema, reason="reserved baseline",
            )  # must not raise

    def test_recording_keeps_reserved(self, pg_db):
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(
            conn, database_config_name="entry", schema_tag=tag,
            physical_schema=f"test_{uuid.uuid4().hex[:8]}", owner="pkg", reserved=True,
        )
        _record_schema_provenance(
            conn, database_config_name="entry", schema_tag=tag,
            new_physical_schema=f"test_{uuid.uuid4().hex[:8]}", reason="move it",
        )
        assert _registry_row(conn, tag).reserved is True

    def test_moving_a_reserved_tag_onto_a_schema_another_owner_uses_raises(self, pg_db):
        tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(
            conn, database_config_name="entry_b", schema_tag=tag,
            physical_schema=f"test_{uuid.uuid4().hex[:8]}", owner="pkg-b", reserved=True,
        )
        _register_schema_claim(
            conn, database_config_name="entry_a", schema_tag=f"tag_{uuid.uuid4().hex[:8]}",
            physical_schema=schema, owner="pkg-a",
        )
        with pytest.raises(SchemaOwnershipError, match=f"{schema!r}.*already used by.*'pkg-a'"):
            _record_schema_provenance(
                conn, database_config_name="entry_b", schema_tag=tag,
                new_physical_schema=schema, reason="move it",
            )

    def test_refuses_a_schema_another_tag_records(self, pg_db):
        other_tag = f"tag_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        conn = pg_db.connection
        _register_schema_claim(conn, database_config_name="entry_a", schema_tag=other_tag, physical_schema=schema)
        with pytest.raises(SchemaDriftError, match=f"Refusing to acknowledge.*entry_a \\({other_tag}\\)"):
            _record_schema_provenance(
                conn, database_config_name="entry_b", schema_tag=f"tag_{uuid.uuid4().hex[:8]}",
                new_physical_schema=schema, reason="take it",
            )


class TestProvenanceClaimAfterAcknowledgment:
    def test_registry_table_created_by_acknowledgment_is_not_pre_existing_data(self, pg_db):
        conn = pg_db.connection
        conn.execute(sa.text("DROP TABLE oa_configurator_provenance.schema_registry"))
        _record_schema_provenance(
            conn, database_config_name="entry", schema_tag=f"tag_{uuid.uuid4().hex[:8]}",
            new_physical_schema=f"test_{uuid.uuid4().hex[:8]}", reason="acknowledged first",
        )
        _register_schema_claim(
            conn, database_config_name="entry", schema_tag="oa_configurator_provenance",
            physical_schema="oa_configurator_provenance", owner="oa_configurator", reserved=True,
        )  # must not raise


def _concurrent_claim_worker(
    url: str, database_config_name: str, schema: str, autocommit: bool = False
) -> str | None:
    """Runs in a separate process: a fresh create_engine() claiming
    schema_tag "primary" for database_config_name, racing several siblings
    doing the same against the same physical database. Returns the error
    message on failure, None on success. Module-level so it's picklable
    for ProcessPoolExecutor.

    Parameters
    ----------
    autocommit : bool, optional
        Pass execution_options={"isolation_level": "AUTOCOMMIT"} through
        create_engine(), as a caller legitimately might for unrelated
        reasons. This cannot be used to bypass the registry lock.
    """
    from oa_configurator import CDMDatabaseConfig
    from oa_configurator.domains.resources.schema import ConnectionConfig
    from oa_configurator.resolver import Resolver
    from oa_configurator.stack_config import StackConfig

    made_url = sa.engine.make_url(url)
    connection_config = ConnectionConfig(
        dialect=made_url.drivername, host=made_url.host, port=made_url.port,
        user=made_url.username, password=made_url.password, database_name=made_url.database,
    )
    stack = StackConfig.for_session(
        connections={"c": connection_config},
        databases={database_config_name: CDMDatabaseConfig(connection="c", cdm_schema=schema)},
    )
    execution_options = {"isolation_level": "AUTOCOMMIT"} if autocommit else None
    try:
        primary, _ = Resolver(stack).resolve_database(database_config_name).create_engines(
            execution_options=execution_options
        )
        primary.dispose()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


class TestConcurrentBootstrap:
    """Regression for the race the registry lock (_lock_schema_registry)
    exists to close: concurrent first-time create_engine() calls used to
    hit UniqueViolation on the registry's own unique index. 8 real OS
    processes, not threads -- the original bug was between separate
    connections/transactions, which threads sharing one Python process
    wouldn't reproduce."""

    @pytest.mark.postgresql
    def test_eight_concurrent_first_time_claims_all_succeed(self, pg_db, pg_connection_config, cleanup_after_test):
        import concurrent.futures

        url = pg_connection_config.build_url()
        database_config_name = f"concurrent_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        cleanup_after_test(lambda: drop_schema_if_exists(pg_db.committing_engine, schema))
        with concurrent.futures.ProcessPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(_concurrent_claim_worker, [url] * 8, [database_config_name] * 8, [schema] * 8)
            )
        failures = [r for r in results if r is not None]
        assert failures == [], f"{len(failures)}/8 workers failed: {failures}"

    @pytest.mark.postgresql
    def test_eight_concurrent_first_time_claims_all_succeed_with_autocommit(self, pg_db, pg_connection_config, cleanup_after_test):
        """A caller-supplied AUTOCOMMIT isolation_level must not defeat
        the registration lock's atomicity."""
        import concurrent.futures

        url = pg_connection_config.build_url()
        database_config_name = f"autocommit_{uuid.uuid4().hex[:8]}"
        schema = f"test_{uuid.uuid4().hex[:8]}"
        cleanup_after_test(lambda: drop_schema_if_exists(pg_db.committing_engine, schema))
        with concurrent.futures.ProcessPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    _concurrent_claim_worker,
                    [url] * 8, [database_config_name] * 8, [schema] * 8, [True] * 8,
                )
            )
        failures = [r for r in results if r is not None]
        assert failures == [], f"{len(failures)}/8 workers failed: {failures}"


class TestOutdatedRegistryLayout:
    def test_a_registry_without_connection_key_raises(self, pg_db):
        conn = pg_db.connection
        conn.execute(sa.text("DROP TABLE oa_configurator_provenance.schema_registry"))
        conn.execute(sa.text(
            "CREATE TABLE oa_configurator_provenance.schema_registry "
            "(id serial primary key, database_name text, schema_tag text)"
        ))
        with pytest.raises(SchemaRegistryOutdatedError, match="outdated layout"):
            _register_schema_claim(
                conn, database_config_name="entry", schema_tag=f"tag_{uuid.uuid4().hex[:8]}",
                physical_schema=f"test_{uuid.uuid4().hex[:8]}",
            )
