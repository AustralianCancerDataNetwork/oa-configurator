"""Tests for resolver.py: Resolver, ResolvedDatabaseTarget, ResolvedResource."""

from __future__ import annotations

import pytest

from oa_configurator import FS_CDM_SCHEMA, FS_DATABASE, Resolver, StackConfig
from oa_configurator.resolver import ResolvedDatabaseTarget, ResolvedResource
from oa_configurator.models import DatabaseConfig


class TestResolveDatabase:
    def test_sqlite_url(self, minimal_stack):
        r = Resolver(minimal_stack)
        target = r.resolve_database("db")
        assert isinstance(target, ResolvedDatabaseTarget)
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
            resources={"default": {FS_DATABASE.name: "db", FS_CDM_SCHEMA.name: "omop"}},
            profiles={
                "test": {
                    "databases": {"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
                }
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
                "default": {
                    FS_DATABASE.name: "cdm",
                    "vocab_database": "vocab",
                    FS_CDM_SCHEMA.name: "omop",
                }
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
            resources={"default": {FS_DATABASE.name: "db", FS_CDM_SCHEMA.name: "base_schema"}},
            profiles={
                "test": {
                    "resources": {
                        "default": {FS_DATABASE.name: "db", FS_CDM_SCHEMA.name: "test_schema"}
                    }
                }
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
            resources={"default": {FS_DATABASE.name: "db", FS_CDM_SCHEMA.name: "omop"}},
            tools={"omop_emb": {"extra": {"backend": "sqlitevec", "path": "/data"}}},
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


class TestResourceAliases:
    def test_alias_resolves_resource(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"my_prod": {FS_DATABASE.name: "db", FS_CDM_SCHEMA.name: "main"}},
            resource_aliases={"cdm_db": "my_prod"},
        )
        r = Resolver(cfg)
        res = r.resolve_resource("cdm_db")
        assert res.cdm_schema == "main"
        assert res.database.name == "db"

    def test_alias_with_profile_override(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"my_prod": {FS_DATABASE.name: "db", FS_CDM_SCHEMA.name: "base"}},
            profiles={
                "test": {
                    "resources": {"my_prod": {FS_DATABASE.name: "db", FS_CDM_SCHEMA.name: "test_schema"}}
                }
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

    def test_no_active_profile(self, minimal_stack):
        r = Resolver(minimal_stack)
        assert r.active_profile_name() is None
