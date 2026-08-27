"""Tests for oa_configurator.testing: isolated_test_database()'s field
resolution and test_only enforcement, dialect dispatch, and the deprecated
compatibility wrappers it's built on top of.

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

from typing import Annotated, ClassVar, cast

import pytest

from oa_configurator import (
    CDMDatabaseConfig,
    ConnectionConfig,
    PackageConfigBase,
    RefTo,
    StackConfig,
)
from oa_configurator.testing import (
    create_fresh_test_db,
    drop_test_db,
    isolated_test_database,
    isolated_test_schema,
    pytest_runtest_setup,
    resolve_test_database,
)
from oa_configurator.testing.base import TestDatabaseStrategy


class DemoTestConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "demo_test_tool"
    test_cdm_db: Annotated[str | None, RefTo(CDMDatabaseConfig, is_test=True)] = None


class DemoTestConfigWithDefault(PackageConfigBase):
    """A test field with a real string default, distinct from its own
    field name, to prove resolve_test_database uses the field's declared
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


class TestResolveTestDatabase:
    """resolve_test_database() is deprecated. Its only wrapper-specific
    behavior, beyond delegating to _resolve_and_check() (covered directly by
    TestResolveAndCheck below), is warning and returning a bare url string
    instead of the resolved object. That is all this class checks."""

    def test_warns_and_returns_the_connection_url(self, monkeypatch):
        cfg = _stack_config(test_only=True)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.warns(DeprecationWarning, match="resolve_test_database"):
            url = resolve_test_database(DemoTestConfig, "test_cdm_db")

        assert url == "sqlite:///:memory:"


class TestRefuseIfProduction:
    """create_fresh_test_db/drop_test_db must refuse to touch a database
    name that matches a non-test_only connection in the stack config, even
    when that connection isn't the one being connected as, isn't wired to
    any database entry, or is only reachable via a secondary field like
    vocab_connection. The guard checks config.connections directly."""

    def test_create_fresh_test_db_refuses_matching_production_connection(
        self, monkeypatch
    ):
        cfg = StackConfig.for_session(
            connections={
                "prod": ConnectionConfig(
                    dialect="postgresql+psycopg",
                    host="dbhost",
                    port=5432,
                    database_name="shared_name",
                    test_only=False,
                ),
            },
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.warns(DeprecationWarning, match="create_fresh_test_db"):
            with pytest.raises(RuntimeError, match="prod"):
                create_fresh_test_db("postgresql+psycopg://user:pw@dbhost:5432/shared_name")

    def test_drop_test_db_refuses_matching_production_connection(self, monkeypatch):
        cfg = StackConfig.for_session(
            connections={
                "prod": ConnectionConfig(
                    dialect="postgresql+psycopg",
                    host="dbhost",
                    port=5432,
                    database_name="shared_name",
                    test_only=False,
                ),
            },
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.warns(DeprecationWarning, match="drop_test_db"):
            with pytest.raises(RuntimeError, match="prod"):
                drop_test_db("postgresql+psycopg://user:pw@dbhost:5432/shared_name")

    def test_refuses_a_production_connection_not_referenced_by_any_database(
        self, monkeypatch
    ):
        """The colliding connection isn't wired to a [databases.*] entry at
        all. Deriving connections from config.databases (the old
        behaviour) would have missed this entirely."""
        cfg = StackConfig.for_session(
            connections={
                "orphan_prod": ConnectionConfig(
                    dialect="postgresql+psycopg",
                    host="dbhost",
                    port=5432,
                    database_name="vocab_name",
                    test_only=False,
                ),
            },
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.warns(DeprecationWarning, match="create_fresh_test_db"):
            with pytest.raises(RuntimeError, match="orphan_prod"):
                create_fresh_test_db("postgresql+psycopg://user:pw@dbhost:5432/vocab_name")


class _FakeMarker:
    def __init__(self, *args):
        self.args = args


class _FakeItem:
    """Minimal stand-in for pytest.Item: only iter_markers is used by
    pytest_runtest_setup."""

    def __init__(self, *database_names: str):
        self._markers = [_FakeMarker(name) for name in database_names]

    def iter_markers(self, name):
        return iter(self._markers) if name == "requires_database" else iter([])


class TestRequiresDatabaseMarker:
    """The requires_database marker must apply the same test_only safety
    check as resolve_test_database, not just check the database resolves."""

    def test_fails_when_database_is_not_test_only(self, monkeypatch):
        cfg = StackConfig.for_session(
            connections={
                "prod": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=False
                )
            },
            databases={"prod_db": CDMDatabaseConfig(connection="prod")},
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.warns(DeprecationWarning, match="requires_database"):
            with pytest.raises(pytest.fail.Exception, match="test_only"):
                pytest_runtest_setup(cast(pytest.Item, _FakeItem("prod_db")))

    def test_skips_when_database_not_configured(self, monkeypatch):
        monkeypatch.setattr(
            "oa_configurator.loader.load_stack_config",
            lambda: StackConfig.for_session(),
        )

        with pytest.warns(DeprecationWarning, match="requires_database"):
            with pytest.raises(pytest.skip.Exception):
                pytest_runtest_setup(cast(pytest.Item, _FakeItem("missing_db")))

    def test_passes_when_test_only(self, monkeypatch):
        cfg = StackConfig.for_session(
            connections={
                "test_conn": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=True
                )
            },
            databases={"test_db": CDMDatabaseConfig(connection="test_conn")},
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.warns(DeprecationWarning, match="requires_database"):
            pytest_runtest_setup(cast(pytest.Item, _FakeItem("test_db")))  # must not raise


class TestIsolatedTestDatabase:
    """isolated_test_database() is the canonical replacement for
    resolve_test_database() plus a hand-built engine. It must enforce the
    same test_only/skip/fail safety, plus actually hand back a working,
    isolated connection/session pair."""

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


class TestIsolatedTestSchema:
    """The narrow, real-commit exception path. SQLite's closest
    equivalent (ATTACH DATABASE) is per-connection state, not visible to a
    genuinely separate connection, so it can't back this primitive's
    cross-connection contract. It must refuse clearly rather than silently
    pass a single-connection test and then fail for real callers."""

    def test_sqlite_raises_not_implemented(self):
        import sqlalchemy as sa

        engine = sa.create_engine("sqlite:///:memory:")
        with pytest.raises(NotImplementedError, match="ATTACH"):
            with isolated_test_schema(engine):
                pass


class TestResolveAndCheck:
    """TestDatabaseStrategy._resolve_and_check() is the shared,
    dialect-agnostic resolution step both isolated_test_database() and the
    deprecated resolve_test_database() are built on. It is covered directly
    here since it's the one place this logic actually lives now. (The
    test_only-enforcement/skip-when-unconfigured behaviors also get
    end-to-end coverage via isolated_test_database() itself, in
    TestIsolatedTestDatabase below. They are not duplicated here.)"""

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

    def test_skips_when_no_config_file_exists(self, monkeypatch):
        """Regression check: a missing config file must skip, not crash.
        load_stack_config() is called twice on this path (once to look up
        the field, once inside Resolver.from_active_config()); both must
        be guarded against FileNotFoundError."""

        def _raise_not_found():
            raise FileNotFoundError("no config file")

        monkeypatch.setattr(
            "oa_configurator.loader.load_stack_config", _raise_not_found
        )

        with pytest.raises(pytest.skip.Exception):
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
