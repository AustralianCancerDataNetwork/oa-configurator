"""Tests for oa_configurator.testing: isolated_test_database()'s field
resolution and test_only enforcement, and dialect dispatch.

TestDatabaseStrategy._resolve_and_check(cls, field_name) is the one place
every consumer routes through to resolve a test database. field_name is
always explicit: a class may eventually have more than one
RefTo(CDMDatabaseConfig, is_test=True) field (e.g. one per backend), and
there is no way to guess which one a caller wants, so the caller always
names it. It does two things: resolves the named field's configured value
(tolerating the rest of the class being unconfigured, see its own docstring
for why), and enforces that the resolved connection is actually marked
test_only=true, refusing to resolve otherwise. That second part is
load-bearing: it's the only thing stopping a misconfigured test field from
silently pointing a destructive test suite at real data.
"""

from __future__ import annotations

from typing import Annotated, ClassVar

import pytest
from sqlalchemy.engine import make_url

from oa_configurator import (
    CDMDatabaseConfig,
    ConnectionConfig,
    Dialect,
    PackageConfigBase,
    RefTo,
    StackConfig,
)
from oa_configurator.config import OAConfiguratorConfig
from oa_configurator.testing import (
    install_postgres_extension,
    isolated_test_database,
    isolated_test_schema,
    scoped_test_schema,
)
from oa_configurator.testing.base import TestDatabaseNotConfigured, TestDatabaseStrategy


class DemoTestConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "demo_test_tool"
    test_cdm_db: Annotated[str | None, RefTo(CDMDatabaseConfig, is_test=True)] = None


class DemoTestConfigWithDefault(PackageConfigBase):
    """A test field with a real string default, distinct from its own
    field name, to prove _resolve_and_check uses the field's declared
    default rather than falling back to the field name itself."""

    tool_name: ClassVar[str] = "demo_test_default_tool"
    test_field: Annotated[str | None, RefTo(CDMDatabaseConfig, is_test=True)] = (
        "configured_default_db"
    )


def _stack_config(*, test_only: bool, tools: dict | None = None) -> StackConfig:
    return StackConfig.for_session(
        connections={
            "test_cdm": ConnectionConfig(
                dialect=Dialect.SQLITE, database_name=":memory:", test_only=test_only
            )
        },
        databases={"test_cdm_db": CDMDatabaseConfig(connection="test_cdm")},
        tools=tools or {},
    )


class TestIsolatedTestDatabase:
    """isolated_test_database() must enforce test_only/skip/fail safety,
    plus actually hand back a working, isolated connection/session pair."""

    def test_yields_a_working_connection_and_session(self, monkeypatch):
        cfg = _stack_config(test_only=True)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with isolated_test_database(DemoTestConfig, "test_cdm_db") as db:
            assert db.connection.execute(pytest.importorskip("sqlalchemy").text("SELECT 1")).scalar() == 1
            assert db.session.connection() is db.connection
            assert db.committing_engine is db.connection.engine

    def test_fails_loudly_when_connection_is_not_test_only(self, monkeypatch):
        cfg = _stack_config(test_only=False)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.fail.Exception, match="SAFETY ABORT"), isolated_test_database(
            DemoTestConfig, "test_cdm_db"
        ):
            pass

    def test_skips_when_database_is_not_configured(self, monkeypatch):
        cfg = StackConfig.for_session()
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.skip.Exception), isolated_test_database(
            DemoTestConfig, "test_cdm_db"
        ):
            pass

class TestIsolatedTestDatabaseDialect:
    """The dialect= parameter: validates a resolved field against an
    expected dialect (always raising on mismatch, never substituting), and
    falls back to a strategy's resolve_without_config() when the field
    isn't configured at all and that dialect supports it."""

    def test_matching_dialect_passes_through(self, monkeypatch):
        cfg = _stack_config(test_only=True)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with isolated_test_database(DemoTestConfig, "test_cdm_db", dialect=Dialect.SQLITE) as db:
            assert db.connection.execute(pytest.importorskip("sqlalchemy").text("SELECT 1")).scalar() == 1

    def test_mismatched_dialect_raises_even_though_configured(self, monkeypatch):
        """test_cdm_db resolves fine (to sqlite) -- a real, configured value,
        not an unconfigured field. Asking for postgresql here is a bug in
        the caller (wrong field for what it's testing), not something to
        route around by substituting a different database."""
        cfg = _stack_config(test_only=True)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(ValueError, match=f"{Dialect.SQLITE.value!r}.*{Dialect.POSTGRESQL.value!r}"), isolated_test_database(
            DemoTestConfig, "test_cdm_db", dialect=Dialect.POSTGRESQL
        ):
            pass

    def test_unconfigured_field_falls_back_to_config_free_dialect(self, monkeypatch):
        cfg = StackConfig.for_session()
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with isolated_test_database(DemoTestConfig, "test_cdm_db", dialect=Dialect.SQLITE) as db:
            assert db.connection.execute(pytest.importorskip("sqlalchemy").text("SELECT 1")).scalar() == 1

    def test_unconfigured_field_still_skips_for_a_dialect_needing_real_config(self, monkeypatch):
        cfg = StackConfig.for_session()
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.skip.Exception), isolated_test_database(
            DemoTestConfig, "test_cdm_db", dialect=Dialect.POSTGRESQL
        ):
            pass

    def test_unknown_dialect_raises_immediately(self, monkeypatch):
        """The message names what was actually resolved and what was
        expected, both as plain dialect values ('sqlite'/'postgresql'),
        not a bare string paired against a Dialect enum's own repr."""
        cfg = _stack_config(test_only=True)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(
            ValueError, match=f"{Dialect.SQLITE.value!r}.*{Dialect.POSTGRESQL.value!r}"
        ), isolated_test_database(DemoTestConfig, "test_cdm_db", dialect=Dialect.POSTGRESQL):
            pass


class TestIsolatedTestDatabaseExtensions:
    """``extensions`` isn't a named parameter of isolated_test_database() /
    isolated_database() -- it reaches create_engine() through **engine_kwargs,
    the one mechanism both dialects use (see install_postgres_extension() for
    the Postgres-specific "install this named extension" callable builder).
    """

    def test_extensions_callable_reaches_create_engine_through_engine_kwargs(self, monkeypatch):
        cfg = StackConfig.for_session()
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)
        calls = []

        with isolated_test_database(
            DemoTestConfig, "test_cdm_db", dialect=Dialect.SQLITE,
            extensions=[lambda conn, record: calls.append(1)],
        ) as db:
            assert db.connection.execute(pytest.importorskip("sqlalchemy").text("SELECT 1")).scalar() == 1

        assert len(calls) == 1


class TestInstallPostgresExtension:
    """install_postgres_extension() builds the extensions-callable that
    replaced the old Postgres-only, string-based pre-install mechanism."""

    def test_callable_runs_quoted_create_extension_and_commits(self):
        class FakeCursor:
            def __init__(self):
                self.executed = []

            def execute(self, query):
                self.executed.append(query.as_string(None))

            def __enter__(self):
                return self

            def __exit__(self, *_exc_info):
                return False

        class FakeConnection:
            def __init__(self):
                self.cursor_obj = FakeCursor()
                self.committed = False

            def cursor(self):
                return self.cursor_obj

            def commit(self):
                self.committed = True

        callback = install_postgres_extension("vector")
        connection = FakeConnection()

        callback(connection, None)

        assert connection.cursor_obj.executed == ['CREATE EXTENSION IF NOT EXISTS "vector"']
        assert connection.committed is True


class TestIsolatedTestSchema:
    """The narrow, real-commit exception path. SQLite's closest
    equivalent (ATTACH DATABASE) is per-connection state, not visible to a
    genuinely separate connection, so it can't back this primitive's
    cross-connection contract. It must refuse clearly rather than silently
    pass a single-connection test and then fail for real callers.

    The three ``test_refuses_*``/``test_sqlite_raises_*`` tests below build
    raw ``sa.create_engine()`` calls deliberately, rather than going
    through ``isolated_test_database()``: they test *this module's own*
    rejection logic against an engine that hasn't been vetted by it, so
    going through the vetted path would make the fixture itself skip/fail
    before the test's own assertion ever ran.
    """

    def test_sqlite_raises_not_implemented(self):
        import sqlalchemy as sa

        engine = sa.create_engine("sqlite:///:memory:")
        with pytest.raises(NotImplementedError, match="ATTACH"), isolated_test_schema(engine):
            pass

    def test_refuses_an_engine_matching_no_known_connection(self, monkeypatch):
        """isolated_test_schema() creates and drops a real, committed
        schema. An engine that doesn't match any connection in the active
        config can't be verified test_only, so it must be refused outright,
        not silently allowed through."""
        import sqlalchemy as sa

        monkeypatch.setattr(
            "oa_configurator.loader.load_stack_config",
            lambda: StackConfig.for_session(),
        )

        engine = sa.create_engine("postgresql+psycopg://user:pw@dbhost:5432/unknown_db")
        with pytest.raises(pytest.fail.Exception, match="SAFETY ABORT"), isolated_test_schema(engine):
            pass

    def test_refuses_an_engine_matching_a_non_test_only_connection(self, monkeypatch):
        cfg = StackConfig.for_session(
            connections={
                "prod": ConnectionConfig(
                    dialect=Dialect.POSTGRESQL + "+psycopg",
                    host="dbhost",
                    port=5432,
                    database_name="prod_db",
                    test_only=False,
                )
            },
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        import sqlalchemy as sa

        engine = sa.create_engine("postgresql+psycopg://user:pw@dbhost:5432/prod_db")
        with pytest.raises(pytest.fail.Exception, match="not marked test_only"), isolated_test_schema(engine):
            pass

    @pytest.mark.postgresql
    @pytest.mark.db_dialect
    def test_creates_and_drops_a_real_schema_for_a_test_only_engine(self, request):
        """A genuinely separate connection can see the schema while it exists,
        and it's gone once the context manager exits. Proves the real point of
        this mechanism, not just that it doesn't raise.

        Uses the documented ``pg_db.connection.engine`` shim (a real,
        independently-connectable Engine, already test_only-vetted by
        isolated_test_database()) rather than hand-building two raw
        engines: ``.connect()`` on the same Engine object twice already
        gives two independent physical connections, which is all this test
        needs to prove cross-connection visibility.
        """
        import sqlalchemy as sa

        with isolated_test_database(OAConfiguratorConfig, "test_db_pg", request=request) as pg_db:
            engine = pg_db.connection.engine
            with isolated_test_schema(engine, prefix="ttest") as schema:
                with engine.connect() as conn:
                    exists = conn.execute(
                        sa.text(
                            "SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"
                        ),
                        {"s": schema},
                    ).scalar()
                assert exists == 1

            with engine.connect() as conn:
                exists_after = conn.execute(
                    sa.text(
                        "SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"
                    ),
                    {"s": schema},
                ).scalar()
            assert exists_after is None


class TestResolveAndCheck:
    """TestDatabaseStrategy._resolve_and_check() is the shared,
    dialect-agnostic resolution step isolated_test_database() is built on."""

    def test_returns_resolved_database_object(self, monkeypatch):
        cfg = _stack_config(test_only=True)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        resolved = TestDatabaseStrategy._resolve_and_check(DemoTestConfig, "test_cdm_db")

        assert make_url(resolved.connection.url) == make_url("sqlite:///:memory:")

    def test_fail_message_names_the_database_and_connection(self, monkeypatch):
        cfg = _stack_config(test_only=False)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.fail.Exception) as exc_info:
            TestDatabaseStrategy._resolve_and_check(DemoTestConfig, "test_cdm_db")

        assert "test_cdm_db" in str(exc_info.value)
        assert "test_cdm" in str(exc_info.value)

    def test_raises_not_configured_when_no_config_file_exists(self, monkeypatch):
        """Regression check: a missing config file must raise TestDatabaseNotConfigured,
        not crash. load_stack_config() is called twice on this path (once to
        look up the field, once inside Resolver.from_active_config()); both
        must be guarded against FileNotFoundError. _resolve_and_check() itself
        no longer skips directly -- isolated_test_database() decides whether
        to skip or try a strategy's resolve_without_config() fallback first
        (covered end-to-end by test_skips_when_database_is_not_configured
        above)."""

        def _raise_not_found():
            raise FileNotFoundError("no config file")

        monkeypatch.setattr(
            "oa_configurator.loader.load_stack_config", _raise_not_found
        )

        with pytest.raises(TestDatabaseNotConfigured):
            TestDatabaseStrategy._resolve_and_check(DemoTestConfig, "test_cdm_db")

    def test_honors_a_configured_override(self, monkeypatch):
        """The whole point of this redesign: if a user configures
        test_cdm_db under a name other than the field's own default, that
        configured name must be what actually gets resolved, not silently
        ignored in favour of the default."""
        cfg = StackConfig.for_session(
            connections={
                "custom_test_conn": ConnectionConfig(
                    dialect=Dialect.SQLITE, database_name=":memory:", test_only=True
                )
            },
            databases={
                "my_custom_test_db": CDMDatabaseConfig(connection="custom_test_conn")
            },
            tools={"demo_test_tool": {"test_cdm_db": "my_custom_test_db"}},
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        resolved = TestDatabaseStrategy._resolve_and_check(DemoTestConfig, "test_cdm_db")

        assert make_url(resolved.connection.url) == make_url("sqlite:///:memory:")

    def test_falls_back_to_field_default_not_field_name(self, monkeypatch):
        """Nothing stored for test_field: must resolve the field's own
        declared default (configured_default_db), not the literal field
        name "test_field"."""
        cfg = StackConfig.for_session(
            connections={
                "test_conn": ConnectionConfig(
                    dialect=Dialect.SQLITE, database_name=":memory:", test_only=True
                )
            },
            databases={
                "configured_default_db": CDMDatabaseConfig(connection="test_conn")
            },
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        resolved = TestDatabaseStrategy._resolve_and_check(
            DemoTestConfigWithDefault, "test_field"
        )

        assert make_url(resolved.connection.url) == make_url("sqlite:///:memory:")

    def test_unknown_field_name_raises(self):
        with pytest.raises(ValueError, match="test_typo"):
            TestDatabaseStrategy._resolve_and_check(DemoTestConfig, "test_typo")

    def test_explicit_config_value_referencing_a_missing_database_raises_not_skips(self, monkeypatch):
        """A config-typo (an explicitly-set value that references no
        [databases.*] entry) must fail loudly."""
        cfg = StackConfig.for_session(
            connections={
                "test_cdm": ConnectionConfig(
                    dialect=Dialect.SQLITE, database_name=":memory:", test_only=True
                )
            },
            databases={"test_cdm_db": CDMDatabaseConfig(connection="test_cdm")},
            tools={"demo_test_tool": {"test_cdm_db": "no_such_database"}},
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(ValueError, match="no_such_database"):
            TestDatabaseStrategy._resolve_and_check(DemoTestConfig, "test_cdm_db")

    def test_unconfigured_field_falling_back_to_its_own_name_still_skips(self, monkeypatch):
        """The field was never set at all (falls back to its own name/default,
        not a user-provided value): still the ordinary "not configured" skip,
        not the loud typo failure above."""
        cfg = StackConfig.for_session(connections={}, databases={})
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(TestDatabaseNotConfigured):
            TestDatabaseStrategy._resolve_and_check(DemoTestConfig, "test_cdm_db")

    def test_injected_resolver_is_used_instead_of_the_active_config(self):
        """A consumer can inject a session-built StackConfig via
        resolver= instead of monkeypatching module.load_stack_config."""
        from oa_configurator.resolver import Resolver

        cfg = _stack_config(test_only=True)
        resolver = Resolver(cfg)

        resolved = TestDatabaseStrategy._resolve_and_check(
            DemoTestConfig, "test_cdm_db", resolver=resolver
        )

        assert make_url(resolved.connection.url) == make_url("sqlite:///:memory:")


class TestResetSchemaRegistryRowsSafety:
    """reset_schema_registry_rows() must refuse an engine whose
    physical database isn't a known test_only connection, since it mutates
    real registry rows."""

    def test_refuses_an_engine_matching_a_non_test_only_connection(self, monkeypatch):
        from oa_configurator.testing import reset_schema_registry_rows

        cfg = StackConfig.for_session(
            connections={
                "prod": ConnectionConfig(
                    dialect=Dialect.POSTGRESQL + "+psycopg",
                    host="dbhost", port=5432, database_name="prod_db", test_only=False,
                )
            },
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        import sqlalchemy as sa

        engine = sa.create_engine("postgresql+psycopg://user:pw@dbhost:5432/prod_db")
        with pytest.raises(pytest.fail.Exception, match="not marked test_only"):
            reset_schema_registry_rows(lambda _fn: None, engine, ["primary"])

    def test_refuses_an_engine_matching_no_known_connection(self, monkeypatch):
        from oa_configurator.testing import reset_schema_registry_rows

        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: StackConfig.for_session())

        import sqlalchemy as sa

        engine = sa.create_engine("postgresql+psycopg://user:pw@unknown-host:5432/unknown_db")
        with pytest.raises(pytest.fail.Exception):
            reset_schema_registry_rows(lambda _fn: None, engine, ["primary"])


@pytest.mark.postgresql
@pytest.mark.db_dialect
class TestResolveWithRoleSchemas:
    """resolve_with_role_schemas() overrides the config entry in memory and resolves it again."""

    def test_unlisted_roles_follow_the_regular_fallback(self, pg_db):
        from oa_configurator import Role
        from oa_configurator.testing import resolve_with_role_schemas

        resolved = resolve_with_role_schemas(pg_db.resolved, {Role.PRIMARY: "ttest_fallback"})
        assert resolved.name == pg_db.resolved.name
        assert {resolved.schema_for_role(role) for role in Role} == {"ttest_fallback"}

    def test_role_without_a_schema_field_raises(self, pg_db):
        from oa_configurator import Role
        from oa_configurator.domains.resources.schema import GenericDatabaseConfig
        from oa_configurator.resolver import Resolver
        from oa_configurator.testing import resolve_with_role_schemas

        name = f"{pg_db.resolved.name}_generic"
        resolver = Resolver.from_active_config().with_overrides(
            databases={name: GenericDatabaseConfig(connection=pg_db.resolved.connection.name)}
        )
        with pytest.raises(ValueError, match="VOCAB"):
            resolve_with_role_schemas(resolver.resolve_database(name), {Role.VOCAB: "x"}, resolver=resolver)


@pytest.mark.postgresql
@pytest.mark.db_dialect
class TestGuardedResolver:
    def test_resolves_the_same_entry_without_test_only(self, pg_db):
        from oa_configurator import Role
        from oa_configurator.testing import guarded_resolver

        resolved = guarded_resolver(pg_db.resolved).resolve_database(pg_db.resolved.name)
        assert resolved.schema_name == pg_db.resolved.schema_name
        assert resolved.connection_for_role(Role.PRIMARY).test_only is False
        assert resolved.connection_for_role(Role.VOCAB).test_only is False


@pytest.mark.postgresql
@pytest.mark.db_dialect
class TestScopedTestSchema:
    """scoped_test_schema() re-points a resolved database at fresh committed
    schemas and builds its engine through create_engine()."""

    @staticmethod
    def _schema_exists(engine, schema: str) -> bool:
        import sqlalchemy as sa

        with engine.connect() as conn:
            return schema in sa.inspect(conn).get_schema_names()

    @staticmethod
    def _registry_rows(engine, schema_tags) -> list:
        import sqlalchemy as sa

        from oa_configurator.domains.resources.schema_registry import (
            SchemaRegistry,
            _with_provenance_translate_map,
        )
        with engine.connect() as conn:
            conn = _with_provenance_translate_map(conn, physical_schema="oa_configurator_provenance")
            return conn.execute(
                sa.select(
                    SchemaRegistry.schema_tag,
                    SchemaRegistry.physical_schema,
                    SchemaRegistry.database_config_name,
                    SchemaRegistry.owner,
                )
                .where(SchemaRegistry.schema_tag.in_(list(schema_tags)))
                .order_by(SchemaRegistry.schema_tag)
            ).all()

    def test_roles_share_one_schema_that_is_dropped_on_exit(self, pg_db):
        from oa_configurator import Role

        with scoped_test_schema(pg_db.resolved, prefix="ttest_shared") as scoped:
            assert set(scoped.schemas) == {Role.PRIMARY, Role.VOCAB, Role.RESULTS}
            schema = scoped.schemas[Role.PRIMARY]
            assert set(scoped.schemas.values()) == {schema}
            assert scoped.resolved.schema_for_role(Role.VOCAB) == schema
            translate_map = scoped.engine.get_execution_options()["schema_translate_map"]
            assert translate_map[Role.PRIMARY.value] == schema
            assert self._schema_exists(pg_db.committing_engine, schema)
        assert not self._schema_exists(pg_db.committing_engine, schema)

    def test_split_role_gets_its_own_schema(self, pg_db):
        from oa_configurator import Role

        with scoped_test_schema(pg_db.resolved, prefix="ttest_split", split_roles=[Role.VOCAB]) as scoped:
            assert scoped.schemas[Role.VOCAB] != scoped.schemas[Role.PRIMARY]
            assert scoped.schemas[Role.RESULTS] == scoped.schemas[Role.PRIMARY]
            translate_map = scoped.engine.get_execution_options()["schema_translate_map"]
            assert translate_map[Role.VOCAB.value] == scoped.schemas[Role.VOCAB]

    def test_separate_vocab_connection_gets_its_own_schema(self, pg_db):
        from oa_configurator import Role
        from oa_configurator.resolver import Resolver

        resolver = Resolver.from_active_config()
        name = pg_db.resolved.name
        resolver = resolver.with_overrides(
            connections={"ttest_vocab_connection": resolver.config.connections[pg_db.resolved.connection.name]},
            databases={
                name: resolver.config.databases[name].model_copy(update={"vocab_connection": "ttest_vocab_connection"})
            },
        )
        with scoped_test_schema(resolver.resolve_database(name), prefix="ttest_conn", resolver=resolver) as scoped:
            assert scoped.resolved.connection_for_role(Role.VOCAB).name == "ttest_vocab_connection"
            assert scoped.schemas[Role.VOCAB] != scoped.schemas[Role.PRIMARY]
            primary, vocab = scoped.resolved.create_engines()
            try:
                # Two connection entries naming one physical database are not a
                # split, so one engine serves both roles and still routes each
                # tag to its own schema. A second pool here would address the
                # database already open as primary.
                assert vocab is primary
                translate_map = primary.get_execution_options()["schema_translate_map"]
                assert translate_map[Role.VOCAB.value] == scoped.schemas[Role.VOCAB]
                assert translate_map[Role.PRIMARY.value] == scoped.schemas[Role.PRIMARY]
            finally:
                primary.dispose()

    def test_generic_database_scopes_only_primary(self, pg_db):
        from oa_configurator import Role
        from oa_configurator.domains.resources.schema import GenericDatabaseConfig
        from oa_configurator.resolver import Resolver

        name = f"{pg_db.resolved.name}_generic"
        resolver = Resolver.from_active_config().with_overrides(
            databases={name: GenericDatabaseConfig(connection=pg_db.resolved.connection.name)}
        )
        with scoped_test_schema(resolver.resolve_database(name), prefix="ttest_generic", resolver=resolver) as scoped:
            assert list(scoped.schemas) == [Role.PRIMARY]
            assert scoped.resolved.schema_name == scoped.schemas[Role.PRIMARY]

    def test_rebaselines_role_rows_to_the_scoped_schema_and_leaves_them_there(self, pg_db):
        """test_only re-baselines on drift, so the row tracks the scoped 
        schema during the block and simply stays there once it exits."""
        from oa_configurator import Role

        for engine in set(pg_db.resolved.create_engines()):
            engine.dispose()
        tags = [role.value for role in Role]
        before = self._registry_rows(pg_db.committing_engine, tags)
        with scoped_test_schema(pg_db.resolved, prefix="ttest_rows") as scoped:
            during = self._registry_rows(pg_db.committing_engine, tags)
            assert any(row[1] == scoped.schemas[Role.PRIMARY] for row in during)
        after = self._registry_rows(pg_db.committing_engine, tags)
        assert after == during
        assert after != before

    def test_caller_claim_is_owned_by_the_calling_package(self, pg_db, cleanup_after_test):
        import uuid

        from oa_configurator import SchemaClaim
        from oa_configurator.testing import reset_schema_registry_rows

        tag = f"ttest_extra_{uuid.uuid4().hex[:8]}"
        reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, [tag])
        claim = SchemaClaim(schema_tag=tag, physical_schema=f"{tag}_schema")
        with scoped_test_schema(pg_db.resolved, prefix="ttest_owner", schema_claims=[claim]):
            pass
        [row] = self._registry_rows(pg_db.committing_engine, [tag])
        assert (row.owner, row.database_config_name) == ("test_testing", pg_db.resolved.name)

    def test_claim_schemas_it_created_are_dropped_and_existing_ones_kept(
        self, pg_db, cleanup_after_test
    ):
        """A caller-created schema is cleaned up, but shared fixed schemas survive."""
        import uuid

        import sqlalchemy as sa

        from oa_configurator import SchemaClaim
        from oa_configurator.testing import (
            drop_schema_if_exists,
            reset_schema_registry_rows,
        )

        created_tag = f"ttest_created_{uuid.uuid4().hex[:8]}"
        kept_tag = f"ttest_kept_{uuid.uuid4().hex[:8]}"
        kept_schema = "oa_configurator_shared_staging"
        reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, [created_tag, kept_tag])
        with pg_db.committing_engine.begin() as connection:
            if kept_schema not in sa.inspect(connection).get_schema_names():
                connection.execute(sa.schema.CreateSchema(kept_schema))
                cleanup_after_test(lambda: drop_schema_if_exists(pg_db.committing_engine, kept_schema))
        claims = [
            SchemaClaim(schema_tag=created_tag, physical_schema=f"{created_tag}_schema"),
            SchemaClaim(schema_tag=kept_tag, physical_schema=kept_schema),
        ]
        with scoped_test_schema(pg_db.resolved, prefix="ttest_claims", schema_claims=claims):
            assert f"{created_tag}_schema" in sa.inspect(pg_db.committing_engine).get_schema_names()
        schemas = set(sa.inspect(pg_db.committing_engine).get_schema_names())
        assert f"{created_tag}_schema" not in schemas
        assert kept_schema in schemas

    def test_reset_registry_rows_restores_them_after_the_test(self, pg_db, cleanup_after_test):
        import uuid

        from oa_configurator.domains.resources.schema_registry import (
            _record_schema_provenance,
        )
        from oa_configurator.testing import reset_schema_registry_rows

        tag = f"ttest_reset_{uuid.uuid4().hex[:8]}"
        reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, [tag])
        with pg_db.committing_engine.begin() as connection:
            _record_schema_provenance(
                connection, database_config_name="ttest", schema_tag=tag,
                new_physical_schema="ttest_schema", reason="reset test",
            )
        before = self._registry_rows(pg_db.committing_engine, [tag])

        teardown = []
        reset_schema_registry_rows(teardown.append, pg_db.committing_engine, [tag])
        assert self._registry_rows(pg_db.committing_engine, [tag]) == []
        teardown[0]()
        assert self._registry_rows(pg_db.committing_engine, [tag]) == before
