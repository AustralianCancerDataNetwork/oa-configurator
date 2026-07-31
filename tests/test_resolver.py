"""Tests for resolver.py: Resolver, ResolvedConnection, ResolvedDatabase, ResolvedProvider, ResolvedModel."""

from __future__ import annotations

import pytest

from oa_configurator import Resolver, StackConfig
from oa_configurator.resolver import ResolvedConnection, ResolvedDatabase, ResolvedModel, ResolvedProvider
from oa_configurator.stack_config import ConnectionConfig, DatabaseConfig, ModelConfig, ProviderConfig


class TestResolveConnection:
    def test_sqlite_url(self, minimal_stack):
        r = Resolver(minimal_stack)
        target = r.resolve_connection("db")
        assert isinstance(target, ResolvedConnection)
        assert target.url == "sqlite:///:memory:"
        assert target.safe_url == "sqlite:///:memory:"

    def test_pg_url_contains_password(self, pg_stack):
        r = Resolver(pg_stack)
        target = r.resolve_connection("cdm")
        assert "secret" in target.url
        assert "secret" not in target.safe_url
        assert "***" in target.safe_url

    def test_unknown_connection_raises(self, minimal_stack):
        r = Resolver(minimal_stack)
        with pytest.raises(KeyError, match="Unknown connection"):
            r.resolve_connection("does_not_exist")


class TestResolveDatabase:
    def test_connection_resolved(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_database("default")
        assert isinstance(res, ResolvedDatabase)
        assert res.connection.name == "db"
        assert res.cdm_schema == "omop"

    def test_vocab_fallback_to_primary(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_database("default")
        assert res.vocab_connection.name == res.connection.name

    def test_vocab_connection_separate(self):
        cfg = StackConfig.for_session(
            connections={
                "cdm": ConnectionConfig(dialect="sqlite", database_name=":memory:"),
                "vocab": ConnectionConfig(dialect="sqlite", database_name=":memory:"),
            },
            databases={
                "default": DatabaseConfig(connection="cdm", vocab_connection="vocab", cdm_schema="omop"),
            },
        )
        r = Resolver(cfg)
        res = r.resolve_database("default")
        assert res.vocab_connection.name == "vocab"

    def test_vocab_schema_falls_back_to_cdm_schema(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_database("default")
        assert res.vocab_schema == "omop"

    def test_explicit_vocab_schema(self, pg_stack):
        r = Resolver(pg_stack)
        res = r.resolve_database("default")
        assert res.vocab_schema == "omop_vocab"
        assert res.results_schema == "results"

    def test_unknown_database_raises(self, minimal_stack):
        r = Resolver(minimal_stack)
        with pytest.raises(KeyError, match="Unknown database"):
            r.resolve_database("does_not_exist")


class TestResolveProvider:
    def test_resolved(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="llamacpp", base_url="http://localhost:8080/v1")},
        )
        r = Resolver(cfg)
        provider = r.resolve_provider("p")
        assert isinstance(provider, ResolvedProvider)
        assert provider.provider == "llamacpp"
        assert provider.base_url == "http://localhost:8080/v1"

    def test_unknown_provider_raises(self):
        cfg = StackConfig.for_session()
        r = Resolver(cfg)
        with pytest.raises(KeyError, match="Unknown provider"):
            r.resolve_provider("does_not_exist")


class TestResolveModel:
    def test_resolved(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="llamacpp", base_url="http://localhost:8080/v1")},
            models={"m": ModelConfig(provider="p", model="local-chat", configuration={"max_tokens": 8000})},
        )
        r = Resolver(cfg)
        model = r.resolve_model("m")
        assert isinstance(model, ResolvedModel)
        assert model.provider.provider == "llamacpp"
        assert model.model == "local-chat"
        assert model.configuration == {"max_tokens": 8000}

    def test_resolved_embedding_fields(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="ollama")},
            models={
                "m": ModelConfig(
                    provider="p",
                    model="nomic-embed-text",
                    embedding_dim=768,
                    document_prefix="search_document: ",
                    query_prefix="search_query: ",
                )
            },
        )
        r = Resolver(cfg)
        model = r.resolve_model("m")
        assert model.embedding_dim == 768
        assert model.document_prefix == "search_document: "
        assert model.query_prefix == "search_query: "

    def test_resolved_embedding_fields_default_to_none(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="ollama")},
            models={"m": ModelConfig(provider="p", model="local-chat")},
        )
        r = Resolver(cfg)
        model = r.resolve_model("m")
        assert model.embedding_dim is None
        assert model.document_prefix is None
        assert model.query_prefix is None

    def test_unknown_model_raises(self):
        cfg = StackConfig.for_session()
        r = Resolver(cfg)
        with pytest.raises(KeyError, match="Unknown model"):
            r.resolve_model("does_not_exist")

    def test_resolved_model_exposes_plain_primitives_for_a_consumer_to_use(self):
        """ResolvedModel is pure data, oa-configurator never imports a consumer package.

        A consumer (e.g. omop-llm) builds whatever it needs from these plain
        fields on its own side, the same way omop_alchemy.config.create_cdm_engine()
        takes a plain ResolvedDatabase and does the omop-alchemy-specific part itself.
        """
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="llamacpp", base_url="http://localhost:8080/v1")},
            models={"m": ModelConfig(provider="p", model="local-chat")},
        )
        r = Resolver(cfg)
        model = r.resolve_model("m")
        assert model.provider.provider == "llamacpp"
        assert model.provider.base_url == "http://localhost:8080/v1"
        assert model.model == "local-chat"


class TestSchemaTranslateMap:
    def test_no_results_schema(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_database("default")
        stm = res.schema_translate_map()
        assert stm[None] == "omop"
        assert "results" not in stm

    def test_with_all_schemas(self, pg_stack):
        r = Resolver(pg_stack)
        res = r.resolve_database("default")
        stm = res.schema_translate_map()
        assert stm[None] == "omop"
        assert stm["vocab"] == "omop_vocab"
        assert stm["results"] == "results"

    def test_vocab_schema_defaults_to_cdm(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_database("default")
        stm = res.schema_translate_map()
        assert stm["vocab"] == "omop"


class TestResolveTool:
    def test_tool_extra_dict(self):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite")},
            databases={"default": DatabaseConfig(connection="db", cdm_schema="omop")},
            tools={"omop_emb": {"backend": "sqlitevec", "path": "/data"}},
        )
        r = Resolver(cfg)
        tool = r.resolve_tool("omop_emb")
        assert tool.extra["backend"] == "sqlitevec"
        assert tool.extra["path"] == "/data"

    def test_unknown_tool_raises(self, minimal_stack):
        r = Resolver(minimal_stack)
        with pytest.raises(KeyError, match="Unknown tool"):
            r.resolve_tool("nonexistent")


class TestCreateEngine:
    def test_sqlite_engine_from_target(self, minimal_stack):
        r = Resolver(minimal_stack)
        target = r.resolve_connection("db")
        engine = target.create_engine()
        assert engine.dialect.name == "sqlite"

    def test_database_create_engine(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_database("default")
        engine = res.create_engine()
        assert engine.dialect.name == "sqlite"


class TestWithOverrides:
    def test_override_connection(self, minimal_stack):
        r = Resolver(minimal_stack)
        r2 = r.with_overrides(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name="/other.db")}
        )
        assert r2.resolve_connection("db").url == "sqlite:////other.db"

    def test_original_unchanged(self, minimal_stack):
        r = Resolver(minimal_stack)
        r.with_overrides(connections={"db": ConnectionConfig(dialect="sqlite", database_name="/other.db")})
        assert r.resolve_connection("db").url == "sqlite:///:memory:"


class TestDiscovery:
    def test_connection_names(self, minimal_stack):
        r = Resolver(minimal_stack)
        assert r.connection_names() == ("db",)

    def test_database_names(self, minimal_stack):
        r = Resolver(minimal_stack)
        assert r.database_names() == ("default",)

    def test_provider_and_model_names(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="ollama")},
            models={"m": ModelConfig(provider="p", model="llama3:8b")},
        )
        r = Resolver(cfg)
        assert r.provider_names() == ("p",)
        assert r.model_names() == ("m",)
