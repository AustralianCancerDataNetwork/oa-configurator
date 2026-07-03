"""Tests for resolver.py: Resolver, ResolvedResource, ResolvedLocalPathKnowledgeResource."""

from __future__ import annotations

import pytest

from pathlib import Path

from oa_configurator import LocalPathKnowledgeResource, Resolver, StackConfig
from oa_configurator.resolver import (
    ResolvedKnowledgeResource,
    ResolvedLocalPathKnowledgeResource,
    ResolvedResource,
)
from oa_configurator.models import DatabaseConfig, ProfileOverrideConfig, ResourceConfig, ToolConfig


class TestResolveDatabase:
    def test_sqlite_url(self, minimal_stack):
        r = Resolver(minimal_stack)
        target = r.resolve_database("db")
        assert isinstance(target, DatabaseConfig)
        assert target.build_url() == "sqlite:///:memory:"
        assert target.safe_url() == "sqlite:///:memory:"

    def test_pg_url_contains_password(self, pg_stack):
        r = Resolver(pg_stack)
        target = r.resolve_database("cdm")
        assert "secret" in target.build_url()
        assert "secret" not in target.safe_url()
        assert "***" in target.safe_url()

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
        assert target.build_url() == "sqlite:///:memory:"


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
            tools={"omop_emb": ToolConfig(extra={"backend": "sqlitevec", "path": "/data"})},
        )
        r = Resolver(cfg)
        tool = r.resolve_tool("omop_emb")
        assert tool.extra["backend"] == "sqlitevec"
        assert tool.extra["path"] == "/data"

    def test_unknown_tool_raises(self, minimal_stack):
        r = Resolver(minimal_stack)
        with pytest.raises(KeyError, match="Unknown tool"):
            r.resolve_tool("nonexistent")


class TestResolveKnowledgeResource:
    def test_resolves_local_path_root(self):
        cfg = StackConfig.for_session(
            knowledge_resources={"default_packs": LocalPathKnowledgeResource(root=Path("/packs"))}
        )
        r = Resolver(cfg)
        resource = r.resolve_knowledge_resource("default_packs")
        assert isinstance(resource, ResolvedLocalPathKnowledgeResource)
        assert isinstance(resource, ResolvedKnowledgeResource)
        assert resource.kind == "local_path"
        assert resource.root == Path("/packs")

    def test_alias_resolves_knowledge_resource(self):
        cfg = StackConfig.for_session(
            knowledge_resources={"site_packs": LocalPathKnowledgeResource(root=Path("/packs"))},
            knowledge_resource_aliases={"default_packs": "site_packs"},
        )
        r = Resolver(cfg)
        resource = r.resolve_knowledge_resource("default_packs")
        assert isinstance(resource, ResolvedLocalPathKnowledgeResource)
        assert resource.root == Path("/packs")

    def test_profile_override_takes_precedence(self):
        cfg = StackConfig.for_session(
            knowledge_resources={"default_packs": LocalPathKnowledgeResource(root=Path("/base"))},
            profiles={
                "test": ProfileOverrideConfig(
                    knowledge_resources={"default_packs": LocalPathKnowledgeResource(root=Path("/profile"))}
                ),
            },
            active_profile="test",
        )
        r = Resolver(cfg)
        resource = r.resolve_knowledge_resource("default_packs")
        assert isinstance(resource, ResolvedLocalPathKnowledgeResource)
        assert resource.root == Path("/profile")

    def test_relative_path_resolves_from_loaded_config(self, tmp_path):
        cfg = StackConfig.for_session(
            knowledge_resources={"default_packs": LocalPathKnowledgeResource(root=Path("knowledge/packs"))}
        )
        cfg.bind_loaded_path(tmp_path / "config.toml")
        r = Resolver(cfg)
        resource = r.resolve_knowledge_resource("default_packs")
        assert isinstance(resource, ResolvedLocalPathKnowledgeResource)
        assert resource.root == (tmp_path / "knowledge" / "packs").resolve()

    def test_unknown_knowledge_resource_raises(self):
        r = Resolver(StackConfig.for_session())
        with pytest.raises(KeyError, match="Unknown knowledge resource"):
            r.resolve_knowledge_resource("missing")

    def test_relative_path_without_loaded_config_warns(self, caplog):
        import logging
        cfg = StackConfig.for_session(
            knowledge_resources={"packs": LocalPathKnowledgeResource(root=Path("relative/packs"))}
        )
        r = Resolver(cfg)
        with caplog.at_level(logging.WARNING, logger="oa_configurator.resolver"):
            r.resolve_knowledge_resource("packs")
        assert any("relative" in msg for msg in caplog.messages)


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
        assert r2.resolve_database("db").build_url() == "sqlite:////other.db"

    def test_original_unchanged(self, minimal_stack):
        r = Resolver(minimal_stack)
        r.with_overrides(databases={"db": DatabaseConfig(dialect="sqlite", database_name="/other.db")})
        assert r.resolve_database("db").build_url() == "sqlite:///:memory:"

    def test_override_knowledge_resource(self):
        r = Resolver(
            StackConfig.for_session(
                knowledge_resources={"default_packs": LocalPathKnowledgeResource(root=Path("/base"))}
            )
        )
        r2 = r.with_overrides(
            knowledge_resources={"default_packs": LocalPathKnowledgeResource(root=Path("/other"))}
        )
        r2_resource = r2.resolve_knowledge_resource("default_packs")
        assert isinstance(r2_resource, ResolvedLocalPathKnowledgeResource)
        assert r2_resource.root == Path("/other")

    def test_override_knowledge_resource_aliases(self):
        r = Resolver(
            StackConfig.for_session(
                knowledge_resources={"site_packs": LocalPathKnowledgeResource(root=Path("/packs"))},
                knowledge_resource_aliases={"default_packs": "site_packs"},
            )
        )
        r2 = r.with_overrides(
            knowledge_resources={"test_packs": LocalPathKnowledgeResource(root=Path("/test"))},
            knowledge_resource_aliases={"default_packs": "test_packs"},
        )
        r2_resource = r2.resolve_knowledge_resource("default_packs")
        assert isinstance(r2_resource, ResolvedLocalPathKnowledgeResource)
        assert r2_resource.root == Path("/test")

    def test_override_resource_aliases(self):
        r = Resolver(
            StackConfig.for_session(
                databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
                resources={"prod": ResourceConfig(database="db", cdm_schema="prod_schema")},
                resource_aliases={"cdm_db": "prod"},
            )
        )
        r2 = r.with_overrides(
            resources={"test": ResourceConfig(database="db", cdm_schema="test_schema")},
            resource_aliases={"cdm_db": "test"},
        )
        assert r2.resolve_resource("cdm_db").cdm_schema == "test_schema"


class TestResourceAliases:
    def test_alias_resolves_resource(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"my_prod": ResourceConfig(database="db", cdm_schema="main")},
            resource_aliases={"cdm_db": "my_prod"},
        )
        r = Resolver(cfg)
        res = r.resolve_resource("cdm_db")
        assert res.cdm_schema == "main"
        assert res.database.name == "db"

    def test_alias_with_profile_override(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"my_prod": ResourceConfig(database="db", cdm_schema="base")},
            profiles={
                "test": ProfileOverrideConfig(
                    resources={"my_prod": ResourceConfig(database="db", cdm_schema="test_schema")},
                ),
            },
            resource_aliases={"cdm_db": "my_prod"},
            active_profile="test",
        )
        r = Resolver(cfg)
        res = r.resolve_resource("cdm_db")
        assert res.cdm_schema == "test_schema"

    def test_unknown_alias_target_raises_at_construction(self):
        with pytest.raises(ValueError, match="resource_aliases"):
            StackConfig.for_session(
                databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
                resources={},
                resource_aliases={"cdm_db": "does_not_exist"},
            )


class TestDiscovery:
    def test_database_names(self, minimal_stack):
        r = Resolver(minimal_stack)
        assert r.database_names() == ("db",)

    def test_resource_names(self, minimal_stack):
        r = Resolver(minimal_stack)
        assert r.resource_names() == ("default",)

    def test_knowledge_resource_names(self):
        r = Resolver(
            StackConfig.for_session(
                knowledge_resources={"default_packs": LocalPathKnowledgeResource(root=Path("/packs"))}
            )
        )
        assert r.knowledge_resource_names() == ("default_packs",)

    def test_no_active_profile(self, minimal_stack):
        r = Resolver(minimal_stack)
        assert r.active_profile_name() is None
