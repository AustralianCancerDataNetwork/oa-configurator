"""Tests for pytest_plugin.py: resolve_test_database's field resolution and
test_only enforcement.

resolve_test_database(cls, field_name) is the one place every consumer
routes through to get a test database's connection URL. field_name is
always explicit -- a class may eventually have more than one
RefTo(DatabaseConfig, is_test=True) field (e.g. one per backend), and
there is no way to guess which one a caller wants, so the caller always
names it. resolve_test_database does two things: resolves the named
field's configured value (tolerating the rest of the class being
unconfigured -- see its own docstring for why), and enforces that the
resolved connection is actually marked test_only=true, refusing to resolve
otherwise. That second part is load-bearing: it's the only thing stopping a
misconfigured test field from silently pointing a destructive test suite at
real data.
"""

from __future__ import annotations

from typing import Annotated, ClassVar

import pytest

from oa_configurator import ConnectionConfig, DatabaseConfig, PackageConfigBase, RefTo, StackConfig
from oa_configurator.pytest_plugin import resolve_test_database


class DemoTestConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "demo_test_tool"
    test_cdm_db: Annotated[str | None, RefTo(DatabaseConfig, is_test=True)] = None


def _stack_config(*, test_only: bool, tools: dict | None = None) -> StackConfig:
    return StackConfig.for_session(
        connections={
            "test_cdm": ConnectionConfig(
                dialect="sqlite", database_name=":memory:", test_only=test_only
            )
        },
        databases={"test_cdm_db": DatabaseConfig(connection="test_cdm")},
        tools=tools or {},
    )


class TestResolveTestDatabase:
    def test_returns_url_when_connection_is_test_only(self, monkeypatch):
        """Field unconfigured -- falls back to the field's own name,
        'test_cdm_db', which is what's set up here."""
        cfg = _stack_config(test_only=True)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        url = resolve_test_database(DemoTestConfig, "test_cdm_db")

        assert url == "sqlite:///:memory:"

    def test_fails_loudly_when_connection_is_not_test_only(self, monkeypatch):
        """A misconfigured test database (pointing at a non-test connection)
        must abort the test run with a clear message, not silently skip --
        skipping would hide the misconfiguration instead of surfacing it."""
        cfg = _stack_config(test_only=False)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.fail.Exception, match="SAFETY ABORT"):
            resolve_test_database(DemoTestConfig, "test_cdm_db")

    def test_fail_message_names_the_database_and_connection(self, monkeypatch):
        cfg = _stack_config(test_only=False)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.fail.Exception) as exc_info:
            resolve_test_database(DemoTestConfig, "test_cdm_db")

        assert "test_cdm_db" in str(exc_info.value)
        assert "test_cdm" in str(exc_info.value)

    def test_skips_when_database_is_not_configured(self, monkeypatch):
        cfg = StackConfig.for_session()
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.skip.Exception):
            resolve_test_database(DemoTestConfig, "test_cdm_db")

    def test_skips_when_no_config_file_exists(self, monkeypatch):
        """Regression check: a missing config file must skip, not crash.
        load_stack_config() is called twice on this path (once to look up
        the field, once inside Resolver.from_active_config()) -- both must
        be guarded against FileNotFoundError."""

        def _raise_not_found():
            raise FileNotFoundError("no config file")

        monkeypatch.setattr("oa_configurator.loader.load_stack_config", _raise_not_found)

        with pytest.raises(pytest.skip.Exception):
            resolve_test_database(DemoTestConfig, "test_cdm_db")

    def test_honors_a_configured_override(self, monkeypatch):
        """The whole point of this redesign: if a user configures
        test_cdm_db under a name other than the field's own default, that
        configured name must be what actually gets resolved -- not silently
        ignored in favour of the default."""
        cfg = StackConfig.for_session(
            connections={
                "custom_test_conn": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=True
                )
            },
            databases={"my_custom_test_db": DatabaseConfig(connection="custom_test_conn")},
            tools={"demo_test_tool": {"test_cdm_db": "my_custom_test_db"}},
        )
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        url = resolve_test_database(DemoTestConfig, "test_cdm_db")

        assert url == "sqlite:///:memory:"
