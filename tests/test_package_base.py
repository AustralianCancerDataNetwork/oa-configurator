"""Tests for package_base.py: PackageConfigBase factory interface."""

from __future__ import annotations

from typing import Annotated, ClassVar

import pytest

from oa_configurator import (
    ConfigurationError,
    ConnectionConfig,
    DatabaseConfig,
    ModelConfig,
    PackageConfigBase,
    ProviderConfig,
    RefTo,
    Resolver,
    StackConfig,
)


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


class DatabaseUserConfig(PackageConfigBase):
    """Stand-in for a package that needs a CDM database: a plain field, no
    separate declaration list -- the field itself is the requirement."""

    tool_name: ClassVar[str] = "database_user_tool"
    cdm_db: Annotated[str, RefTo(DatabaseConfig)] = "cdm_db"


class EmbeddingConfig(PackageConfigBase):
    """Stand-in for a package with its own field naming a [models.*] entry."""

    tool_name: ClassVar[str] = "embedding_tool"
    embedding_model_name: Annotated[str, RefTo(ModelConfig)] = "embed-default"


class TestRefToPackageField:
    """resolve_package_config validates a package's own RefTo-marked fields --
    the same mechanism for databases and models, no ResourceSpec/required_resources
    needed."""

    def test_passes_when_referenced_database_exists(self):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"cdm_db": DatabaseConfig(connection="db")},
        )
        result = Resolver(cfg).resolve_package_config(DatabaseUserConfig)
        assert result.cdm_db == "cdm_db"

    def test_raises_when_referenced_database_missing(self):
        cfg = StackConfig.for_session()
        with pytest.raises(ConfigurationError) as exc_info:
            Resolver(cfg).resolve_package_config(DatabaseUserConfig)
        msg = str(exc_info.value)
        assert "cdm_db" in msg
        assert "omop-config configure database_user_tool" in msg

    def test_passes_when_referenced_model_exists(self):
        cfg = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="ollama")},
            models={"embed-default": ModelConfig(provider="p", model="nomic-embed-text")},
        )
        result = Resolver(cfg).resolve_package_config(EmbeddingConfig)
        assert result.embedding_model_name == "embed-default"

    def test_raises_when_referenced_model_missing(self):
        cfg = StackConfig.for_session()
        with pytest.raises(ConfigurationError) as exc_info:
            Resolver(cfg).resolve_package_config(EmbeddingConfig)
        msg = str(exc_info.value)
        assert "embed-default" in msg
        assert "omop-config configure embedding_tool" in msg


class OtherDatabaseUserConfig(PackageConfigBase):
    """A second, unrelated package whose field happens to default to the
    same database name as DatabaseUserConfig -- no import of that class."""

    tool_name: ClassVar[str] = "other_database_user_tool"
    cdm_database: Annotated[str, RefTo(DatabaseConfig)] = "cdm_db"


class TestConventionBasedSharing:
    """Two packages share a database purely by their fields' default values
    matching -- no cross-package reference object of any kind."""

    def test_two_packages_resolve_to_the_same_database(self):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"cdm_db": DatabaseConfig(connection="db")},
        )
        a = Resolver(cfg).resolve_package_config(DatabaseUserConfig)
        b = Resolver(cfg).resolve_package_config(OtherDatabaseUserConfig)
        assert a.cdm_db == b.cdm_database == "cdm_db"
