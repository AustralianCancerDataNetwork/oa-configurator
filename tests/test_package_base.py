"""Tests for package_base.py: PackageConfigBase factory interface."""

from __future__ import annotations

from typing import ClassVar

import pytest

from oa_configurator import (
    ConfigurationError,
    PackageConfigBase,
    Resolver,
    ResourceRef,
    ResourceSpec,
    StackConfig,
    DatabaseConfig,
    ResourceConfig,
)
from oa_configurator.models import ProfileOverrideConfig


class SampleConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "sample_tool"
    backend: str = "default_backend"
    file_path: str | None = None


class TestPackageConfigBase:
    def test_resolve_package_config_reads_extra(self):
        cfg = StackConfig.for_session(
            tools={"sample_tool": {"backend": "custom", "file_path": "/data"}}
        )
        sample = Resolver(cfg).resolve_package_config(SampleConfig)
        assert sample.backend == "custom"
        assert sample.file_path == "/data"

    def test_resolve_package_config_uses_defaults_when_tool_missing(self):
        cfg = StackConfig.for_session()
        sample = Resolver(cfg).resolve_package_config(SampleConfig)
        assert sample.backend == "default_backend"
        assert sample.file_path is None

    def test_resolve_package_config_uses_defaults_when_extra_empty(self):
        cfg = StackConfig.for_session(
            tools={"sample_tool": {}}
        )
        sample = Resolver(cfg).resolve_package_config(SampleConfig)
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
            tools={"sample_tool": extra}
        )
        restored = Resolver(cfg).resolve_package_config(SampleConfig)
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
        result = Resolver(cfg).resolve_package_config(RequiredConfig)
        assert result.value == "default_value"

    def test_raises_when_resource_missing(self):
        cfg = StackConfig.for_session()
        with pytest.raises(ConfigurationError) as exc_info:
            Resolver(cfg).resolve_package_config(RequiredConfig)
        msg = str(exc_info.value)
        assert "cdm_db" in msg
        assert "omop-config configure required_tool" in msg

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
        result = Resolver(cfg).resolve_package_config(RequiredConfig)
        assert result.value == "default_value"

    def test_empty_required_resources_always_passes(self):
        cfg = StackConfig.for_session()
        result = Resolver(cfg).resolve_package_config(SampleConfig)
        assert result.backend == "default_backend"


class OwnerConfig(PackageConfigBase):
    """Stand-in for another package that owns a resource, e.g. omop_alchemy."""

    CDM_DB: ClassVar[ResourceSpec] = ResourceSpec(
        semantic_name="cdm_db",
        display_name="CDM Database",
        description="Owned by OwnerConfig.",
    )
    tool_name: ClassVar[str] = "owner_tool"
    owned_resources: ClassVar[tuple[ResourceSpec, ...]] = (CDM_DB,)


class ConsumerConfig(PackageConfigBase):
    """Stand-in for a package consuming another package's owned resource."""

    tool_name: ClassVar[str] = "consumer_tool"
    required_resources: ClassVar[tuple[ResourceRef, ...]] = (
        ResourceRef(OwnerConfig, OwnerConfig.CDM_DB),
    )


class TestResourceRef:
    def test_resolves_through_owning_package(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"cdm_db": ResourceConfig(database="db", cdm_schema="main")},
        )
        result = Resolver(cfg).resolve_package_config(ConsumerConfig)
        assert result is not None

    def test_error_names_the_owning_package_not_the_consumer(self):
        cfg = StackConfig.for_session()
        with pytest.raises(ConfigurationError) as exc_info:
            Resolver(cfg).resolve_package_config(ConsumerConfig)
        msg = str(exc_info.value)
        assert "cdm_db" in msg
        assert "omop-config configure owner_tool" in msg
        assert "consumer_tool" not in msg

    def test_resolve_engine_accepts_resource_ref(self):
        cfg = StackConfig.for_session(
            databases={"db": DatabaseConfig(dialect="sqlite", database_name=":memory:")},
            resources={"cdm_db": ResourceConfig(database="db", cdm_schema="main")},
        )
        engine = Resolver(cfg).resolve_engine(ResourceRef(OwnerConfig, OwnerConfig.CDM_DB))
        assert engine is not None
