"""Tests for models.py: DatabaseConfig, ResourceConfig, ToolConfig, StackConfig."""

from __future__ import annotations
from pathlib import Path

import pytest

from oa_configurator import (
    DatabaseConfig,
    KnowledgeResourceConfig,
    LocalPathKnowledgeResource,
    ResourceConfig,
    StackConfig,
    ToolConfig,
)
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


class TestLocalPathKnowledgeResource:
    def test_minimal(self):
        r = LocalPathKnowledgeResource(root=Path("/packs"))
        assert r.kind == "local_path"

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            LocalPathKnowledgeResource(root=Path("/packs"), unknown="x") # type: ignore

    def test_is_subclass_of_base(self):
        assert issubclass(LocalPathKnowledgeResource, KnowledgeResourceConfig)


class TestToolConfig:
    def test_defaults(self):
        t = ToolConfig()
        assert t.default_resource is None
        assert t.default_knowledge_resource is None
        assert t.extra == {}

    def test_extra_accepts_any_values(self):
        t = ToolConfig(extra={"backend": "sqlitevec", "path": "/data"})
        assert t.extra["backend"] == "sqlitevec"


class TestStackConfig:
    def test_for_session_minimal(self, minimal_stack):
        assert "db" in minimal_stack.databases
        assert "default" in minimal_stack.resources

    def test_for_session_accepts_raw_dicts(self):
        """Raw, TOML-table-shaped dicts (not ResourceConfig instances) still coerce at validation time."""
        cfg = StackConfig.for_session(
            databases={"c": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"r": {"database": "c", "cdm_schema": "s"}},  # type: ignore[dict-item]
            knowledge_resources={"k": {"kind": "local_path", "root": "/packs"}},  # type: ignore[dict-item]
        )
        assert isinstance(cfg.databases["c"], DatabaseConfig)
        assert isinstance(cfg.resources["r"], ResourceConfig)
        assert isinstance(cfg.knowledge_resources["k"], KnowledgeResourceConfig)

    def test_cross_ref_validation_unknown_database(self):
        with pytest.raises(ValueError, match="unknown database"):
            StackConfig.for_session(
                databases={},
                resources={"r": ResourceConfig(database="missing", cdm_schema="s")},
            )

    def test_cross_ref_validation_unknown_resource_in_tool(self):
        with pytest.raises(ValueError, match="unknown resource"):
            StackConfig.for_session(
                databases={"c": DatabaseConfig(dialect="sqlite")},
                resources={},
                tools={"t": ToolConfig(default_resource="missing")},
            )

    def test_cross_ref_validation_unknown_knowledge_resource_in_tool(self):
        with pytest.raises(ValueError, match="unknown knowledge resource"):
            StackConfig.for_session(
                databases={"c": DatabaseConfig(dialect="sqlite")},
                knowledge_resources={},
                tools={"t": ToolConfig(default_knowledge_resource="missing")},
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
            knowledge_resources={"k": LocalPathKnowledgeResource(root=Path("/packs"))},
            profiles={
                "test": ProfileOverrideConfig(
                    databases={"tc": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
                    resources={"r": ResourceConfig(database="tc", cdm_schema="s")},
                    knowledge_resources={"k": LocalPathKnowledgeResource(root=Path("/profile-packs"))},
                ),
            },
        )
        assert "tc" in cfg.profiles["test"].databases
        assert "k" in cfg.profiles["test"].knowledge_resources

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

    def test_unknown_knowledge_resource_alias_target_raises_at_construction(self):
        with pytest.raises(ValueError, match="knowledge_resource_aliases"):
            StackConfig.for_session(
                knowledge_resources={},
                knowledge_resource_aliases={"default_packs": "does_not_exist"},
            )

    def test_knowledge_resource_names(self):
        cfg = StackConfig.for_session(
            knowledge_resources={"default_packs": LocalPathKnowledgeResource(root=Path("/packs"))}
        )
        assert cfg.knowledge_resource_names() == ("default_packs",)

    def test_extra_fields_forbidden(self):
        with pytest.raises(Exception):
            StackConfig(unknown_top_level="x")  # type: ignore
