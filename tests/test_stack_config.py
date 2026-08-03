"""Tests for the domain schemas and stack_config.py: ConnectionConfig, DatabaseConfig, StackConfig."""

from __future__ import annotations

import pytest

from oa_configurator import ConnectionConfig, DatabaseConfig, ModelConfig, ProviderConfig, StackConfig


class TestConnectionConfig:
    def test_sqlite_build_url(self):
        db = ConnectionConfig(dialect="sqlite", database_name=":memory:")
        assert db.build_url() == "sqlite:///:memory:"

    def test_sqlite_default_database(self):
        db = ConnectionConfig(dialect="sqlite")
        assert db.build_url() == "sqlite:///:memory:"

    def test_pg_build_url_includes_password(self):
        db = ConnectionConfig(
            dialect="postgresql+psycopg",
            host="localhost",
            port=5432,
            user="admin",
            password="s3cret",
            database_name="mydb",
        )
        url = db.build_url()
        assert "s3cret" in url
        assert "localhost" in url
        assert "mydb" in url

    def test_pg_safe_url_redacts_password(self):
        db = ConnectionConfig(
            dialect="postgresql+psycopg",
            host="localhost",
            user="admin",
            password="s3cret",
            database_name="mydb",
        )
        safe = db.safe_url()
        assert "s3cret" not in safe
        assert "***" in safe

    def test_dialect_required(self):
        with pytest.raises(Exception):
            ConnectionConfig()  # type: ignore

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            ConnectionConfig(dialect="sqlite", unknown_field="x")  # type: ignore


class TestDatabaseConfig:
    def test_minimal(self):
        r = DatabaseConfig(connection="db", cdm_schema="omop")
        assert r.vocab_connection is None
        assert r.vocab_schema is None
        assert r.results_schema is None

    def test_cdm_schema_defaults_to_omop(self):
        r = DatabaseConfig(connection="db")
        assert r.cdm_schema == "omop"

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            DatabaseConfig(connection="db", cdm_schema="omop", unknown="x")  # type: ignore


class TestStackConfig:
    def test_for_session_minimal(self, minimal_stack):
        assert "db" in minimal_stack.connections
        assert "default" in minimal_stack.databases

    def test_tools_accepts_plain_dicts(self):
        cfg = StackConfig.for_session(tools={"my_pkg": {"backend": "sqlitevec", "path": "/data"}})
        assert cfg.tools["my_pkg"]["backend"] == "sqlitevec"

    def test_for_session_accepts_raw_dicts(self):
        """Raw, TOML-table-shaped dicts (not DatabaseConfig instances) still coerce at validation time."""
        cfg = StackConfig.for_session(
            connections={"c": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"r": {"connection": "c", "cdm_schema": "s"}},  # ty: ignore[invalid-argument-type]
        )
        assert isinstance(cfg.connections["c"], ConnectionConfig)
        assert isinstance(cfg.databases["r"], DatabaseConfig)

    def test_cross_ref_validation_unknown_connection(self):
        with pytest.raises(ValueError, match="unknown connection"):
            StackConfig.for_session(
                connections={},
                databases={"r": DatabaseConfig(connection="missing", cdm_schema="s")},
            )

    def test_cross_ref_validation_vocab_connection(self):
        with pytest.raises(ValueError, match="unknown connection"):
            StackConfig.for_session(
                connections={"c": ConnectionConfig(dialect="sqlite")},
                databases={"r": DatabaseConfig(connection="c", vocab_connection="missing", cdm_schema="s")},
            )

    def test_bind_loaded_path(self, tmp_path):
        cfg = StackConfig.for_session()
        cfg.bind_loaded_path(tmp_path / "config.toml")
        assert cfg.loaded_path == tmp_path / "config.toml"

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            StackConfig(unknown_top_level="x")  # type: ignore

    def test_cross_ref_validation_unknown_provider(self):
        with pytest.raises(ValueError, match="unknown provider"):
            StackConfig.for_session(
                providers={},
                models={"m": ModelConfig(provider="missing", model="llama3:8b")},
            )


class TestProviderConfig:
    def test_minimal(self):
        provider = ProviderConfig(provider="ollama")
        assert provider.provider == "ollama"
        assert provider.base_url is None
        assert provider.api_key is None

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            ProviderConfig(provider="ollama", unknown_field="x")  # type: ignore

    def test_provider_required(self):
        with pytest.raises(Exception):
            ProviderConfig()  # type: ignore


class TestModelConfig:
    def test_minimal(self):
        model = ModelConfig(provider="p", model="llama3:8b")
        assert model.provider == "p"
        assert model.model == "llama3:8b"
        assert model.embedding_dim is None
        assert model.document_prefix is None
        assert model.query_prefix is None
        assert model.configuration == {}

    def test_embedding_fields(self):
        model = ModelConfig(
            provider="p",
            model="nomic-embed-text",
            embedding_dim=768,
            document_prefix="search_document: ",
            query_prefix="search_query: ",
        )
        assert model.embedding_dim == 768
        assert model.document_prefix == "search_document: "
        assert model.query_prefix == "search_query: "

    def test_configuration_defaults_independent_per_instance(self):
        a = ModelConfig(provider="p", model="m1")
        b = ModelConfig(provider="p", model="m2")
        a.configuration["x"] = 1
        assert b.configuration == {}

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            ModelConfig(provider="p", model="m", unknown_field="x")  # type: ignore
