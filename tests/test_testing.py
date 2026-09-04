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

from oa_configurator import (
    CDMDatabaseConfig,
    ConnectionConfig,
    PackageConfigBase,
    RefTo,
    StackConfig,
)
from oa_configurator.config import OAConfiguratorConfig
from oa_configurator.testing.base import TestDatabaseNotConfigured
from oa_configurator.testing import isolated_test_database, isolated_test_schema
from oa_configurator.testing.base import TestDatabaseStrategy


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
                dialect="sqlite", database_name=":memory:", test_only=test_only
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

    def test_fails_loudly_when_connection_is_not_test_only(self, monkeypatch):
        cfg = _stack_config(test_only=False)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.fail.Exception, match="SAFETY ABORT"):
            with isolated_test_database(DemoTestConfig, "test_cdm_db"):
                pass

    def test_skips_when_database_is_not_configured(self, monkeypatch):
        cfg = StackConfig.for_session()
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.skip.Exception):
            with isolated_test_database(DemoTestConfig, "test_cdm_db"):
                pass

    def test_unregistered_dialect_raises_not_implemented_error(self, monkeypatch):
        """A dialect with no registered TestDatabaseStrategy (e.g. mssql, in
        this codebase today) must fail clearly, naming what IS supported.
        It must not silently pick the wrong strategy or crash obscurely."""
        cfg = StackConfig.for_session(
            connections={
                "test_mssql": ConnectionConfig(
                    dialect="mssql+pyodbc",
                    host="dbhost",
                    database_name="test_db",
                    test_only=True,
                )
            },
            databases={"test_cdm_db": CDMDatabaseConfig(connection="test_mssql")},
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(NotImplementedError, match="mssql"):
            with isolated_test_database(DemoTestConfig, "test_cdm_db"):
                pass


class TestIsolatedTestDatabaseDialect:
    """The dialect= parameter: validates a resolved field against an
    expected dialect (always raising on mismatch, never substituting), and
    falls back to a strategy's resolve_without_config() when the field
    isn't configured at all and that dialect supports it."""

    def test_matching_dialect_passes_through(self, monkeypatch):
        cfg = _stack_config(test_only=True)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with isolated_test_database(DemoTestConfig, "test_cdm_db", dialect="sqlite") as db:
            assert db.connection.execute(pytest.importorskip("sqlalchemy").text("SELECT 1")).scalar() == 1

    def test_mismatched_dialect_raises_even_though_configured(self, monkeypatch):
        """test_cdm_db resolves fine (to sqlite) -- a real, configured value,
        not an unconfigured field. Asking for postgresql here is a bug in
        the caller (wrong field for what it's testing), not something to
        route around by substituting a different database."""
        cfg = _stack_config(test_only=True)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(ValueError, match="sqlite.*postgresql"):
            with isolated_test_database(DemoTestConfig, "test_cdm_db", dialect="postgresql"):
                pass

    def test_unconfigured_field_falls_back_to_config_free_dialect(self, monkeypatch):
        cfg = StackConfig.for_session()
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with isolated_test_database(DemoTestConfig, "test_cdm_db", dialect="sqlite") as db:
            assert db.connection.execute(pytest.importorskip("sqlalchemy").text("SELECT 1")).scalar() == 1

    def test_unconfigured_field_still_skips_for_a_dialect_needing_real_config(self, monkeypatch):
        cfg = StackConfig.for_session()
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.skip.Exception):
            with isolated_test_database(DemoTestConfig, "test_cdm_db", dialect="postgresql"):
                pass

    def test_unknown_dialect_raises_immediately(self, monkeypatch):
        cfg = _stack_config(test_only=True)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(ValueError, match="postgres.*sqlite"):
            with isolated_test_database(DemoTestConfig, "test_cdm_db", dialect="postgres"):
                pass


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
        with pytest.raises(NotImplementedError, match="ATTACH"):
            with isolated_test_schema(engine):
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
        with pytest.raises(pytest.fail.Exception, match="SAFETY ABORT"):
            with isolated_test_schema(engine):
                pass

    def test_refuses_an_engine_matching_a_non_test_only_connection(self, monkeypatch):
        cfg = StackConfig.for_session(
            connections={
                "prod": ConnectionConfig(
                    dialect="postgresql+psycopg",
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
        with pytest.raises(pytest.fail.Exception, match="not marked test_only"):
            with isolated_test_schema(engine):
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

        assert resolved.connection.url == "sqlite:///:memory:"

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
                    dialect="sqlite", database_name=":memory:", test_only=True
                )
            },
            databases={
                "my_custom_test_db": CDMDatabaseConfig(connection="custom_test_conn")
            },
            tools={"demo_test_tool": {"test_cdm_db": "my_custom_test_db"}},
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        resolved = TestDatabaseStrategy._resolve_and_check(DemoTestConfig, "test_cdm_db")

        assert resolved.connection.url == "sqlite:///:memory:"

    def test_falls_back_to_field_default_not_field_name(self, monkeypatch):
        """Nothing stored for test_field: must resolve the field's own
        declared default (configured_default_db), not the literal field
        name "test_field"."""
        cfg = StackConfig.for_session(
            connections={
                "test_conn": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=True
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

        assert resolved.connection.url == "sqlite:///:memory:"

    def test_unknown_field_name_raises(self):
        with pytest.raises(ValueError, match="test_typo"):
            TestDatabaseStrategy._resolve_and_check(DemoTestConfig, "test_typo")
