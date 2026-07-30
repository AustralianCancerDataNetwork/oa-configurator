"""Tests for models.py: DatabaseConfig, ResourceConfig, StackConfig."""

from __future__ import annotations

import pytest

from oa_configurator import DatabaseConfig, ModelConfig, ProviderConfig, ResourceConfig, StackConfig
from oa_configurator.models import ProfileOverrideConfig


class TestDatabaseConfig:
    def test_sqlite_build_url(self):
        db = DatabaseConfig(dialect="sqlite", database_name=":memory:")
        assert db.build_url() == "sqlite:///:memory:"

    def test_sqlite_default_database(self):
        db = DatabaseConfig(dialect="sqlite")
        assert db.build_url() == "sqlite:///:memory:"

    def test_pg_build_url_includes_password(self):
        db = DatabaseConfig(
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
        db = DatabaseConfig(
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
            DatabaseConfig()  # type: ignore

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            DatabaseConfig(dialect="sqlite", unknown_field="x")  # type: ignore

    def test_read_only_flag(self):
        db = DatabaseConfig(dialect="sqlite", database_name=":memory:", read_only=True)
        assert db.read_only is True


class TestResourceConfig:
    def test_minimal(self):
        r = ResourceConfig(database="db", cdm_schema="omop")
        assert r.vocab_database is None
        assert r.vocab_schema is None
        assert r.results_schema is None

    def test_cdm_schema_required(self):
        with pytest.raises(Exception):
            ResourceConfig(database="db")  # type: ignore

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            ResourceConfig(database="db", cdm_schema="omop", unknown="x")  # type: ignore


class TestStackConfig:
    def test_for_session_minimal(self, minimal_stack):
        assert "db" in minimal_stack.databases
        assert "default" in minimal_stack.resources

    def test_tools_accepts_plain_dicts(self):
        cfg = StackConfig.for_session(tools={"my_pkg": {"backend": "sqlitevec", "path": "/data"}})
        assert cfg.tools["my_pkg"]["backend"] == "sqlitevec"

    def test_for_session_accepts_raw_dicts(self):
        """Raw, TOML-table-shaped dicts (not ResourceConfig instances) still coerce at validation time."""
        cfg = StackConfig.for_session(
            databases={"c": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"r": {"database": "c", "cdm_schema": "s"}},  # type: ignore[dict-item]
        )
        assert isinstance(cfg.databases["c"], DatabaseConfig)
        assert isinstance(cfg.resources["r"], ResourceConfig)

    def test_cross_ref_validation_unknown_database(self):
        with pytest.raises(ValueError, match="unknown database"):
            StackConfig.for_session(
                databases={},
                resources={"r": ResourceConfig(database="missing", cdm_schema="s")},
            )

    def test_cross_ref_validation_vocab_database(self):
        with pytest.raises(ValueError, match="unknown database"):
            StackConfig.for_session(
                databases={"c": DatabaseConfig(dialect="sqlite")},
                resources={"r": ResourceConfig(database="c", vocab_database="missing", cdm_schema="s")},
            )

    def test_profile_overlay_cross_ref(self):
        cfg = StackConfig.for_session(
            databases={"c": DatabaseConfig(dialect="sqlite")},
            resources={"r": ResourceConfig(database="c", cdm_schema="s")},
            profiles={
                "test": ProfileOverrideConfig(
                    databases={"tc": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
                    resources={"r": ResourceConfig(database="tc", cdm_schema="s")},
                ),
            },
        )
        assert "tc" in cfg.profiles["test"].databases

    def test_profile_overlay_invalid_cross_ref(self):
        with pytest.raises(ValueError, match="unknown database"):
            StackConfig.for_session(
                databases={"c": DatabaseConfig(dialect="sqlite")},
                resources={"r": ResourceConfig(database="c", cdm_schema="s")},
                profiles={
                    "bad": ProfileOverrideConfig(
                        resources={"r": ResourceConfig(database="nonexistent", cdm_schema="s")},
                    ),
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

    def test_cross_ref_validation_unknown_provider(self):
        with pytest.raises(ValueError, match="unknown provider"):
            StackConfig.for_session(
                providers={},
                models={"m": ModelConfig(provider="missing", model="llama3:8b")},
            )

    def test_model_profile_overlay_cross_ref(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="ollama")},
            models={"m": ModelConfig(provider="p", model="llama3:8b")},
            profiles={
                "test": ProfileOverrideConfig(
                    providers={"tp": ProviderConfig(provider="anthropic")},
                    models={"m": ModelConfig(provider="tp", model="claude-sonnet-4")},
                ),
            },
        )
        assert "tp" in cfg.profiles["test"].providers

    def test_model_profile_overlay_invalid_cross_ref(self):
        with pytest.raises(ValueError, match="unknown provider"):
            StackConfig.for_session(
                providers={"p": ProviderConfig(provider="ollama")},
                models={"m": ModelConfig(provider="p", model="llama3:8b")},
                profiles={
                    "bad": ProfileOverrideConfig(
                        models={"m": ModelConfig(provider="nonexistent", model="llama3:8b")},
                    ),
                },
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
