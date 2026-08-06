"""Tests for the domain schemas and stack_config.py: ConnectionConfig, DatabaseConfig, StackConfig."""

from __future__ import annotations

import pytest

from oa_configurator import (
    CDMDatabaseConfig,
    ConnectionConfig,
    DatabaseConfig,
    DatabaseKind,
    GenericDatabaseConfig,
    ModelConfig,
    ProviderConfig,
    StackConfig,
)
from oa_configurator.stack_config import mismatched_kind_refs


class TestConnectionConfig:
    def test_sqlite_build_url(self):
        db = ConnectionConfig(dialect="sqlite", database_name=":memory:")
        assert db.build_url() == "sqlite:///:memory:"

    def test_sqlite_without_database_name_raises(self):
        """No implicit ':memory:' fallback: an unset database_name for a
        sqlite dialect would otherwise silently discard data on every
        restart, with no indication anything was ever in-memory."""
        db = ConnectionConfig(dialect="sqlite")
        with pytest.raises(ValueError, match="database_name"):
            db.build_url()

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


class TestGenericDatabaseConfig:
    def test_minimal(self):
        r = GenericDatabaseConfig(connection="db")
        assert r.kind == DatabaseKind.GENERIC
        assert r.schema_name is None

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            GenericDatabaseConfig(connection="db", vocab_schema="x")  # type: ignore

    def test_kind_cannot_be_overridden(self):
        with pytest.raises(Exception):
            GenericDatabaseConfig(connection="db", kind="cdm")  # type: ignore


class TestCDMDatabaseConfig:
    def test_minimal(self):
        r = CDMDatabaseConfig(connection="db")
        assert r.kind == DatabaseKind.CDM
        assert r.vocab_connection is None
        assert r.vocab_schema is None
        assert r.results_schema is None

    def test_schema_name_defaults_to_omop(self):
        r = CDMDatabaseConfig(connection="db")
        assert r.schema_name == "omop"

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            CDMDatabaseConfig(connection="db", unknown="x")  # type: ignore


class TestDatabaseKindDiscrimination:
    def test_missing_kind_rejected(self):
        with pytest.raises(Exception, match="kind"):
            StackConfig.for_session(
                connections={"c": ConnectionConfig(dialect="sqlite")},
                databases={"r": {"connection": "c"}},  # ty: ignore[invalid-argument-type]
            )

    def test_raw_dict_dispatches_by_kind(self):
        cfg = StackConfig.for_session(
            connections={"c": ConnectionConfig(dialect="sqlite")},
            databases={
                "g": {"kind": "generic", "connection": "c"},  # ty: ignore[invalid-argument-type]
                "d": {"kind": "cdm", "connection": "c"},  # ty: ignore[invalid-argument-type]
            },
        )
        assert isinstance(cfg.databases["g"], GenericDatabaseConfig)
        assert isinstance(cfg.databases["d"], CDMDatabaseConfig)

    def test_unknown_kind_rejected(self):
        with pytest.raises(Exception):
            StackConfig.for_session(
                connections={"c": ConnectionConfig(dialect="sqlite")},
                databases={"r": {"kind": "bogus", "connection": "c"}},  # ty: ignore[invalid-argument-type]
            )


class TestMismatchedKindRefs:
    def test_no_op_for_matching_kind(self, pg_stack):
        db = pg_stack.databases["default"]
        assert mismatched_kind_refs(db, pg_stack) == []

    def test_flags_wrong_subtype(self):
        cfg = StackConfig.for_session(
            connections={"c": ConnectionConfig(dialect="sqlite")},
            databases={"g": GenericDatabaseConfig(connection="c")},
        )
        from oa_configurator.domains.vector_stores.schema import VectorStoreConfig

        vs = VectorStoreConfig(backend_type="pgvector", database="g")
        assert mismatched_kind_refs(vs, cfg) == []

        cfg2 = StackConfig.for_session(
            connections={"c": ConnectionConfig(dialect="sqlite")},
            databases={"d": CDMDatabaseConfig(connection="c")},
        )
        vs2 = VectorStoreConfig(backend_type="pgvector", database="d")
        problems = mismatched_kind_refs(vs2, cfg2)
        assert len(problems) == 1
        field_name, value, expected, actual = problems[0]
        assert field_name == "database"
        assert value == "d"
        assert expected is GenericDatabaseConfig
        assert actual is CDMDatabaseConfig


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
            databases={"r": {"connection": "c", "kind": "cdm", "schema_name": "s"}},  # ty: ignore[invalid-argument-type]
        )
        assert isinstance(cfg.connections["c"], ConnectionConfig)
        assert isinstance(cfg.databases["r"], DatabaseConfig)

    def test_cross_ref_validation_unknown_connection(self):
        with pytest.raises(ValueError, match="unknown connection"):
            StackConfig.for_session(
                connections={},
                databases={"r": CDMDatabaseConfig(connection="missing", schema_name="s")},
            )

    def test_cross_ref_validation_vocab_connection(self):
        with pytest.raises(ValueError, match="unknown connection"):
            StackConfig.for_session(
                connections={"c": ConnectionConfig(dialect="sqlite")},
                databases={"r": CDMDatabaseConfig(connection="c", vocab_connection="missing", schema_name="s")},
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
