"""Tests for package_base.py: PackageConfigBase factory interface."""

from __future__ import annotations

from typing import ClassVar

import pytest

from oa_configurator import (
    ConfigurationError,
    KnowledgeResourceConfig,
    PackageConfigBase,
    StackConfig,
    DatabaseConfig,
    ResourceConfig,
    ToolConfig,
)
from oa_configurator.models import ProfileOverrideConfig


class SampleConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "sample_tool"
    backend: str = "default_backend"
    file_path: str | None = None


class TestPackageConfigBase:
    def test_from_stack_reads_extra(self):
        cfg = StackConfig.for_session(
            tools={"sample_tool": ToolConfig(extra={"backend": "custom", "file_path": "/data"})}
        )
        sample = SampleConfig.from_stack(cfg)
        assert sample.backend == "custom"
        assert sample.file_path == "/data"

    def test_from_stack_uses_defaults_when_tool_missing(self):
        cfg = StackConfig.for_session()
        sample = SampleConfig.from_stack(cfg)
        assert sample.backend == "default_backend"
        assert sample.file_path is None

    def test_from_stack_uses_defaults_when_extra_empty(self):
        cfg = StackConfig.for_session(
            tools={"sample_tool": ToolConfig(extra={})}
        )
        sample = SampleConfig.from_stack(cfg)
        assert sample.backend == "default_backend"

    def test_to_extra_dict_excludes_none(self):
        sample = SampleConfig(backend="custom", file_path=None)
        d = sample.to_extra_dict()
        assert d == {"backend": "custom"}
        assert "file_path" not in d

    def test_to_extra_dict_includes_set_values(self):
        sample = SampleConfig(backend="custom", file_path="/data")
        d = sample.to_extra_dict()
        assert d == {"backend": "custom", "file_path": "/data"}

    def test_round_trip(self):
        original = SampleConfig(backend="sqlitevec", file_path="/embeddings")
        extra = original.to_extra_dict()
        cfg = StackConfig.for_session(
            tools={"sample_tool": ToolConfig(extra=extra)}
        )
        restored = SampleConfig.from_stack(cfg)
        assert restored.backend == "sqlitevec"
        assert restored.file_path == "/embeddings"

    def test_subclass_must_set_tool_name(self):
        with pytest.raises((AttributeError, TypeError)):
            class BadConfig(PackageConfigBase):
                pass
            # Accessing tool_name on an instance should fail
            BadConfig().tool_name  # type: ignore[attr-defined]


class RequiredConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "required_tool"
    required_resources: ClassVar[tuple[str, ...]] = ("cdm_db",)
    value: str = "default_value"


class TestRequiredResources:
    def test_passes_when_resource_present(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"cdm_db": ResourceConfig(database="db", cdm_schema="main")},
        )
        result = RequiredConfig.from_stack(cfg)
        assert result.value == "default_value"

    def test_raises_when_resource_missing(self):
        cfg = StackConfig.for_session()
        with pytest.raises(ConfigurationError) as exc_info:
            RequiredConfig.from_stack(cfg)
        msg = str(exc_info.value)
        assert "cdm_db" in msg
        assert "omop-config configure required_tool" in msg

    def test_raises_includes_alias_hint(self):
        cfg = StackConfig.for_session()
        with pytest.raises(ConfigurationError) as exc_info:
            RequiredConfig.from_stack(cfg)
        msg = str(exc_info.value)
        assert "[resource_aliases]" in msg
        assert 'cdm_db = "your-resource-name"' in msg

    def test_passes_when_resource_aliased(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"my_prod": ResourceConfig(database="db", cdm_schema="main")},
            resource_aliases={"cdm_db": "my_prod"},
        )
        result = RequiredConfig.from_stack(cfg)
        assert result.value == "default_value"

    def test_respects_default_resource_override(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"my_custom": ResourceConfig(database="db", cdm_schema="main")},
            tools={"required_tool": ToolConfig(default_resource="my_custom")},
        )
        result = RequiredConfig.from_stack(cfg)
        assert result.value == "default_value"

    def test_recognises_profile_resources(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            profiles={
                "test": ProfileOverrideConfig(
                    resources={"cdm_db": ResourceConfig(database="db", cdm_schema="test_schema")},
                ),
            },
            active_profile="test",
        )
        result = RequiredConfig.from_stack(cfg)
        assert result.value == "default_value"

    def test_empty_required_resources_always_passes(self):
        cfg = StackConfig.for_session()
        result = SampleConfig.from_stack(cfg)
        assert result.backend == "default_backend"


class RequiredKnowledgeConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "required_knowledge_tool"
    required_knowledge_resources: ClassVar[tuple[str, ...]] = ("default_packs",)
    value: str = "default_value"


class TestRequiredKnowledgeResources:
    def test_passes_when_knowledge_resource_present(self):
        cfg = StackConfig.for_session(
            knowledge_resources={"default_packs": KnowledgeResourceConfig(root="/packs")}
        )
        result = RequiredKnowledgeConfig.from_stack(cfg)
        assert result.value == "default_value"

    def test_raises_when_knowledge_resource_missing(self):
        cfg = StackConfig.for_session()
        with pytest.raises(ConfigurationError) as exc_info:
            RequiredKnowledgeConfig.from_stack(cfg)
        msg = str(exc_info.value)
        assert "default_packs" in msg
        assert "knowledge resource" in msg

    def test_passes_when_knowledge_resource_aliased(self):
        cfg = StackConfig.for_session(
            knowledge_resources={"org_packs": KnowledgeResourceConfig(root="/packs")},
            knowledge_resource_aliases={"default_packs": "org_packs"},
        )
        result = RequiredKnowledgeConfig.from_stack(cfg)
        assert result.value == "default_value"

    def test_respects_default_knowledge_resource_override(self):
        cfg = StackConfig.for_session(
            knowledge_resources={"my_custom": KnowledgeResourceConfig(root="/packs")},
            tools={"required_knowledge_tool": ToolConfig(default_knowledge_resource="my_custom")},
        )
        result = RequiredKnowledgeConfig.from_stack(cfg)
        assert result.value == "default_value"

    def test_recognises_profile_knowledge_resources(self):
        cfg = StackConfig.for_session(
            profiles={
                "test": ProfileOverrideConfig(
                    knowledge_resources={"default_packs": KnowledgeResourceConfig(root="/packs")},
                ),
            },
            active_profile="test",
        )
        result = RequiredKnowledgeConfig.from_stack(cfg)
        assert result.value == "default_value"

    def test_get_knowledge_resource_raises_when_none_declared(self, monkeypatch):
        monkeypatch.setattr(
            "oa_configurator.loader.load_stack_config",
            lambda: StackConfig.for_session(),
        )
        with pytest.raises(ConfigurationError, match="has no knowledge resources"):
            SampleConfig.get_knowledge_resource()
