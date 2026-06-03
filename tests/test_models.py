"""Tests for models.py: ConnectionConfig, ResourceConfig, ToolConfig, StackConfig."""

from __future__ import annotations

import pytest

from oa_configurator import ConnectionConfig, ResourceConfig, StackConfig, ToolConfig


class TestConnectionConfig:
    def test_sqlite_build_url(self):
        conn = ConnectionConfig(dialect="sqlite", database=":memory:")
        assert conn.build_url() == "sqlite:///:memory:"

    def test_sqlite_default_database(self):
        conn = ConnectionConfig(dialect="sqlite")
        assert conn.build_url() == "sqlite:///:memory:"

    def test_pg_build_url_includes_password(self):
        conn = ConnectionConfig(
            dialect="postgresql+psycopg",
            host="localhost",
            port=5432,
            user="admin",
            password="s3cret",
            database="mydb",
        )
        url = conn.build_url()
        assert "s3cret" in url
        assert "localhost" in url
        assert "mydb" in url

    def test_pg_safe_url_redacts_password(self):
        conn = ConnectionConfig(
            dialect="postgresql+psycopg",
            host="localhost",
            user="admin",
            password="s3cret",
            database="mydb",
        )
        safe = conn.safe_url()
        assert "s3cret" not in safe
        assert "***" in safe

    def test_dialect_required(self):
        with pytest.raises(Exception):
            ConnectionConfig()  # type: ignore

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            ConnectionConfig(dialect="sqlite", unknown_field="x") # type: ignore

    def test_read_only_flag(self):
        conn = ConnectionConfig(dialect="sqlite", database=":memory:", read_only=True)
        assert conn.read_only is True


class TestResourceConfig:
    def test_minimal(self):
        r = ResourceConfig(primary_db="db", cdm_schema="omop")
        assert r.vocab_db is None
        assert r.vocab_schema is None
        assert r.results_schema is None

    def test_cdm_schema_required(self):
        with pytest.raises(Exception):
            ResourceConfig(primary_db="db")  # type: ignore

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            ResourceConfig(primary_db="db", cdm_schema="omop", unknown="x") # type: ignore


class TestToolConfig:
    def test_defaults(self):
        t = ToolConfig()
        assert t.default_resource is None
        assert t.extra == {}

    def test_extra_accepts_any_values(self):
        t = ToolConfig(extra={"backend": "sqlitevec", "path": "/data"})
        assert t.extra["backend"] == "sqlitevec"


class TestStackConfig:
    def test_for_session_minimal(self, minimal_stack):
        assert "db" in minimal_stack.connections
        assert "default" in minimal_stack.resources

    def test_for_session_accepts_raw_dicts(self):
        cfg = StackConfig.for_session(
            connections={"c": {"dialect": "sqlite", "database": ":memory:"}},
            resources={"r": {"primary_db": "c", "cdm_schema": "s"}},
        )
        assert isinstance(cfg.connections["c"], ConnectionConfig)
        assert isinstance(cfg.resources["r"], ResourceConfig)

    def test_cross_ref_validation_unknown_connection(self):
        with pytest.raises(ValueError, match="unknown connection"):
            StackConfig.for_session(
                connections={},
                resources={"r": {"primary_db": "missing", "cdm_schema": "s"}},
            )

    def test_cross_ref_validation_unknown_resource_in_tool(self):
        with pytest.raises(ValueError, match="unknown resource"):
            StackConfig.for_session(
                connections={"c": {"dialect": "sqlite"}},
                resources={},
                tools={"t": {"default_resource": "missing"}},
            )

    def test_cross_ref_validation_vocab_db(self):
        with pytest.raises(ValueError, match="unknown connection"):
            StackConfig.for_session(
                connections={"c": {"dialect": "sqlite"}},
                resources={"r": {"primary_db": "c", "vocab_db": "missing", "cdm_schema": "s"}},
            )

    def test_profile_overlay_cross_ref(self):
        cfg = StackConfig.for_session(
            connections={"c": {"dialect": "sqlite"}},
            resources={"r": {"primary_db": "c", "cdm_schema": "s"}},
            profiles={
                "test": {
                    "connections": {"tc": {"dialect": "sqlite", "database": ":memory:"}},
                    "resources": {"r": {"primary_db": "tc", "cdm_schema": "s"}},
                }
            },
        )
        assert "tc" in cfg.profiles["test"].connections

    def test_profile_overlay_invalid_cross_ref(self):
        with pytest.raises(ValueError, match="unknown connection"):
            StackConfig.for_session(
                connections={"c": {"dialect": "sqlite"}},
                resources={"r": {"primary_db": "c", "cdm_schema": "s"}},
                profiles={
                    "bad": {
                        "resources": {"r": {"primary_db": "nonexistent", "cdm_schema": "s"}},
                    }
                },
            )

    def test_active_profile_none_by_default(self):
        cfg = StackConfig.for_session()
        assert cfg.active_profile is None

    def test_bind_loaded_path(self, tmp_path):
        cfg = StackConfig.for_session()
        cfg.bind_loaded_path(tmp_path / "config.toml")
        assert cfg.loaded_path == tmp_path / "config.toml"

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            StackConfig(unknown_top_level="x")  # type: ignore
