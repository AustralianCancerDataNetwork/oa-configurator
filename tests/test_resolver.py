"""Tests for resolver.py: Resolver, ResolvedDatabase, ResolvedResource, ResolvedProvider, ResolvedModel."""

from __future__ import annotations

import pytest

from oa_configurator import Resolver, StackConfig
from oa_configurator.resolver import ResolvedDatabase, ResolvedModel, ResolvedProvider, ResolvedResource
from oa_configurator.models import DatabaseConfig, ModelConfig, ProfileOverrideConfig, ProviderConfig, ResourceConfig


class TestResolveDatabase:
    def test_sqlite_url(self, minimal_stack):
        r = Resolver(minimal_stack)
        target = r.resolve_database("db")
        assert isinstance(target, ResolvedDatabase)
        assert target.url == "sqlite:///:memory:"
        assert target.safe_url == "sqlite:///:memory:"

    def test_pg_url_contains_password(self, pg_stack):
        r = Resolver(pg_stack)
        target = r.resolve_database("cdm")
        assert "secret" in target.url
        assert "secret" not in target.safe_url
        assert "***" in target.safe_url

    def test_unknown_database_raises(self, minimal_stack):
        r = Resolver(minimal_stack)
        with pytest.raises(KeyError, match="Unknown database"):
            r.resolve_database("does_not_exist")

    def test_profile_database_takes_precedence(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name="/base.db")},
            resources={"default": ResourceConfig(database="db", cdm_schema="omop")},
            profiles={
                "test": ProfileOverrideConfig(
                    databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
                ),
            },
            active_profile="test",
        )
        r = Resolver(cfg)
        target = r.resolve_database("db")
        assert target.url == "sqlite:///:memory:"


class TestResolveResource:
    def test_database_resolved(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_resource("default")
        assert isinstance(res, ResolvedResource)
        assert res.database.name == "db"
        assert res.cdm_schema == "omop"

    def test_vocab_fallback_to_primary(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_resource("default")
        assert res.vocab_database.name == res.database.name

    def test_vocab_database_separate(self):
        cfg = StackConfig.for_session(
            databases={
                "cdm": DatabaseConfig(dialect="sqlite", database_name=":memory:"),
                "vocab": DatabaseConfig(dialect="sqlite", database_name=":memory:"),
            },
            resources={
                "default": ResourceConfig(database="cdm", vocab_database="vocab", cdm_schema="omop"),
            },
        )
        r = Resolver(cfg)
        res = r.resolve_resource("default")
        assert res.vocab_database.name == "vocab"

    def test_vocab_schema_falls_back_to_cdm_schema(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_resource("default")
        assert res.vocab_schema == "omop"

    def test_explicit_vocab_schema(self, pg_stack):
        r = Resolver(pg_stack)
        res = r.resolve_resource("default")
        assert res.vocab_schema == "omop_vocab"
        assert res.results_schema == "results"

    def test_unknown_resource_raises(self, minimal_stack):
        r = Resolver(minimal_stack)
        with pytest.raises(KeyError, match="Unknown resource"):
            r.resolve_resource("does_not_exist")

    def test_profile_resource_takes_precedence(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"default": ResourceConfig(database="db", cdm_schema="base_schema")},
            profiles={
                "test": ProfileOverrideConfig(
                    resources={"default": ResourceConfig(database="db", cdm_schema="test_schema")},
                ),
            },
            active_profile="test",
        )
        r = Resolver(cfg)
        res = r.resolve_resource("default")
        assert res.cdm_schema == "test_schema"


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

    def test_profile_provider_takes_precedence(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="ollama")},
            profiles={
                "test": ProfileOverrideConfig(
                    providers={"p": ProviderConfig(provider="anthropic")},
                ),
            },
            active_profile="test",
        )
        r = Resolver(cfg)
        provider = r.resolve_provider("p")
        assert provider.provider == "anthropic"


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
        takes a plain ResolvedResource and does the omop-alchemy-specific part itself.
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

    def test_profile_model_takes_precedence(self):
        cfg = StackConfig.for_session(
            providers={
                "local": ProviderConfig(provider="llamacpp", base_url="http://localhost:8080/v1"),
                "cloud": ProviderConfig(provider="anthropic"),
            },
            models={"m": ModelConfig(provider="local", model="local-model")},
            profiles={
                "test": ProfileOverrideConfig(
                    models={"m": ModelConfig(provider="cloud", model="claude-sonnet-4")},
                ),
            },
            active_profile="test",
        )
        r = Resolver(cfg)
        model = r.resolve_model("m")
        assert model.provider.provider == "anthropic"
        assert model.model == "claude-sonnet-4"


class TestSchemaTranslateMap:
    def test_no_results_schema(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_resource("default")
        stm = res.schema_translate_map()
        assert stm[None] == "omop"
        assert "results" not in stm

    def test_with_all_schemas(self, pg_stack):
        r = Resolver(pg_stack)
        res = r.resolve_resource("default")
        stm = res.schema_translate_map()
        assert stm[None] == "omop"
        assert stm["vocab"] == "omop_vocab"
        assert stm["results"] == "results"

    def test_vocab_schema_defaults_to_cdm(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_resource("default")
        stm = res.schema_translate_map()
        assert stm["vocab"] == "omop"


class TestResolveTool:
    def test_tool_extra_dict(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite")},
            resources={"default": ResourceConfig(database="db", cdm_schema="omop")},
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
        target = r.resolve_database("db")
        engine = target.create_engine()
        assert engine.dialect.name == "sqlite"

    def test_resource_create_engine(self, minimal_stack):
        r = Resolver(minimal_stack)
        res = r.resolve_resource("default")
        engine = res.create_engine()
        assert engine.dialect.name == "sqlite"


class TestWithOverrides:
    def test_override_database(self, minimal_stack):
        r = Resolver(minimal_stack)
        r2 = r.with_overrides(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name="/other.db")}
        )
        assert r2.resolve_database("db").url == "sqlite:////other.db"

    def test_original_unchanged(self, minimal_stack):
        r = Resolver(minimal_stack)
        r.with_overrides(databases={"db": DatabaseConfig(dialect="sqlite", database_name="/other.db")})
        assert r.resolve_database("db").url == "sqlite:///:memory:"


class TestEffectiveResourceNames:
    def test_base_only(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"cdm_db": ResourceConfig(database="db", cdm_schema="main")},
        )
        assert Resolver(cfg).effective_resource_names() == frozenset({"cdm_db"})

    def test_includes_profile_overlay(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"cdm_db": ResourceConfig(database="db", cdm_schema="main")},
            profiles={
                "test": ProfileOverrideConfig(
                    resources={"test_db": ResourceConfig(database="db", cdm_schema="test_schema")},
                ),
            },
            active_profile="test",
        )
        assert Resolver(cfg).effective_resource_names() == {"cdm_db", "test_db"}


class TestResolveToolConfigProfileOverlay:
    """Confirms resolve_package_config applies profile overlays -- the bug
    PackageConfigBase.from_stack()'s independent lookup used to miss."""

    def test_profile_tool_override_takes_precedence(self):
        from oa_configurator import PackageConfigBase
        from typing import ClassVar

        class SampleConfig(PackageConfigBase):
            tool_name: ClassVar[str] = "sample_tool"
            backend: str = "default_backend"

        cfg = StackConfig.for_session(
            tools={"sample_tool": {"backend": "base"}},
            profiles={
                "test": ProfileOverrideConfig(
                    tools={"sample_tool": {"backend": "overridden"}},
                ),
            },
            active_profile="test",
        )
        result = Resolver(cfg).resolve_package_config(SampleConfig)
        assert result.backend == "overridden"


class TestDiscovery:
    def test_database_names(self, minimal_stack):
        r = Resolver(minimal_stack)
        assert r.database_names() == ("db",)

    def test_resource_names(self, minimal_stack):
        r = Resolver(minimal_stack)
        assert r.resource_names() == ("default",)

    def test_provider_and_model_names(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="ollama")},
            models={"m": ModelConfig(provider="p", model="llama3:8b")},
        )
        r = Resolver(cfg)
        assert r.provider_names() == ("p",)
        assert r.model_names() == ("m",)

    def test_no_active_profile(self, minimal_stack):
        r = Resolver(minimal_stack)
        assert r.active_profile_name() is None
