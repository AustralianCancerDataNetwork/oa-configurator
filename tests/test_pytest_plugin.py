"""Tests for pytest_plugin.py: resolve_test_database's test_only enforcement.

resolve_test_database() is the one place every consumer routes through to get
a test database's connection URL. Its safety check -- refusing to resolve
unless the underlying connection is marked test_only=true -- is load-bearing:
it's the only thing stopping a misconfigured test field from silently
pointing a destructive test suite at real data.
"""

from __future__ import annotations

import pytest

from oa_configurator import ConnectionConfig, DatabaseConfig, StackConfig
from oa_configurator.pytest_plugin import resolve_test_database


def _stack_config(*, test_only: bool) -> StackConfig:
    return StackConfig.for_session(
        connections={
            "test_cdm": ConnectionConfig(
                dialect="sqlite", database_name=":memory:", test_only=test_only
            )
        },
        databases={"test_cdm_db": DatabaseConfig(connection="test_cdm")},
    )


class TestResolveTestDatabase:
    def test_returns_url_when_connection_is_test_only(self, monkeypatch):
        cfg = _stack_config(test_only=True)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        url = resolve_test_database("test_cdm_db")

        assert url == "sqlite:///:memory:"

    def test_fails_loudly_when_connection_is_not_test_only(self, monkeypatch):
        """A misconfigured test database (pointing at a non-test connection)
        must abort the test run with a clear message, not silently skip --
        skipping would hide the misconfiguration instead of surfacing it."""
        cfg = _stack_config(test_only=False)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.fail.Exception, match="SAFETY ABORT"):
            resolve_test_database("test_cdm_db")

    def test_fail_message_names_the_database_and_connection(self, monkeypatch):
        cfg = _stack_config(test_only=False)
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.fail.Exception) as exc_info:
            resolve_test_database("test_cdm_db")

        assert "test_cdm_db" in str(exc_info.value)
        assert "test_cdm" in str(exc_info.value)

    def test_skips_when_database_is_not_configured(self, monkeypatch):
        cfg = StackConfig.for_session()
        monkeypatch.setattr("oa_configurator.loader.load_stack_config", lambda: cfg)

        with pytest.raises(pytest.skip.Exception):
            resolve_test_database("test_cdm_db")
