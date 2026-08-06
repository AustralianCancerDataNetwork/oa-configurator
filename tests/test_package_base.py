"""Tests for package_base.py: PackageConfigBase factory interface."""

from __future__ import annotations

from typing import Annotated, ClassVar

import pytest

from oa_configurator import (
    CDMDatabaseConfig,
    ConfigurationError,
    ConnectionConfig,
    DatabaseConfig,
    ModelConfig,
    PackageConfigBase,
    ProviderConfig,
    RefTo,
    Resolver,
    StackConfig,
    UnknownRefTarget,
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
    separate declaration list. The field itself is the requirement."""

    tool_name: ClassVar[str] = "database_user_tool"
    cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"


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
            databases={"cdm_db": CDMDatabaseConfig(connection="db")},
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
    same database name as DatabaseUserConfig. No import of that class."""

    tool_name: ClassVar[str] = "other_database_user_tool"
    cdm_database: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"


class TestConventionBasedSharing:
    """Two packages share a database purely by their fields' default values
    matching. No cross-package reference object of any kind is involved."""

    def test_two_packages_resolve_to_the_same_database(self):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"cdm_db": CDMDatabaseConfig(connection="db")},
        )
        a = Resolver(cfg).resolve_package_config(DatabaseUserConfig)
        b = Resolver(cfg).resolve_package_config(OtherDatabaseUserConfig)
        assert a.cdm_db == b.cdm_database == "cdm_db"


class TestRefToIsTest:
    """is_test lives on RefTo itself now: a field self-declares its
    test-ness instead of the framework inferring it from whether the
    field's own Python name happens to start with "test_"."""

    def test_defaults_to_false(self):
        assert RefTo(CDMDatabaseConfig).is_test is False

    def test_explicit_true(self):
        assert RefTo(CDMDatabaseConfig, is_test=True).is_test is True

    def test_field_name_has_no_bearing_on_is_test(self):
        """A field named without any "test_" prefix can still be is_test,
        and one named with the prefix can still default to False. The
        Python attribute name carries no meaning anymore."""

        class OddlyNamedConfig(PackageConfigBase):
            tool_name: ClassVar[str] = "oddly_named_tool"
            playground_db: Annotated[str | None, RefTo(CDMDatabaseConfig, is_test=True)] = None
            test_flavoured_prod_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "prod_db"

        fields = dict(OddlyNamedConfig.model_fields)
        playground_ref = next(m for m in fields["playground_db"].metadata if isinstance(m, RefTo))
        prod_ref = next(m for m in fields["test_flavoured_prod_db"].metadata if isinstance(m, RefTo))
        assert playground_ref.is_test is True
        assert prod_ref.is_test is False


class TestIsTestOnlyMatchEnforcement:
    """resolve_package_config checks that a field's is_test declaration
    agrees with the test_only flag on whatever connection it resolves to,
    in both directions."""

    def test_test_field_pointed_at_real_connection_raises(self):
        cfg = StackConfig.for_session(
            connections={"prod": ConnectionConfig(dialect="sqlite", database_name=":memory:", test_only=False)},
            databases={"test_cdm_db": CDMDatabaseConfig(connection="prod")},
        )

        class NeedsTestDb(PackageConfigBase):
            tool_name: ClassVar[str] = "needs_test_db_tool"
            test_cdm_db: Annotated[str | None, RefTo(CDMDatabaseConfig, is_test=True)] = "test_cdm_db"

        with pytest.raises(ConfigurationError, match="is_test=True"):
            Resolver(cfg).resolve_package_config(NeedsTestDb)

    def test_prod_field_pointed_at_test_only_connection_raises(self):
        cfg = StackConfig.for_session(
            connections={"test_conn": ConnectionConfig(dialect="sqlite", database_name=":memory:", test_only=True)},
            databases={"cdm_db": CDMDatabaseConfig(connection="test_conn")},
        )

        class NeedsProdDb(PackageConfigBase):
            tool_name: ClassVar[str] = "needs_prod_db_tool"
            cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"

        with pytest.raises(ConfigurationError, match="is_test=False"):
            Resolver(cfg).resolve_package_config(NeedsProdDb)

    def test_matching_is_test_and_test_only_passes(self):
        cfg = StackConfig.for_session(
            connections={"test_conn": ConnectionConfig(dialect="sqlite", database_name=":memory:", test_only=True)},
            databases={"test_cdm_db": CDMDatabaseConfig(connection="test_conn")},
        )

        class NeedsTestDb(PackageConfigBase):
            tool_name: ClassVar[str] = "matching_test_db_tool"
            test_cdm_db: Annotated[str | None, RefTo(CDMDatabaseConfig, is_test=True)] = "test_cdm_db"

        result = Resolver(cfg).resolve_package_config(NeedsTestDb)
        assert result.test_cdm_db == "test_cdm_db"

    def test_matching_non_test_passes(self):
        cfg = StackConfig.for_session(
            connections={"prod": ConnectionConfig(dialect="sqlite", database_name=":memory:", test_only=False)},
            databases={"cdm_db": CDMDatabaseConfig(connection="prod")},
        )

        class NeedsProdDb(PackageConfigBase):
            tool_name: ClassVar[str] = "matching_prod_db_tool"
            cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"

        result = Resolver(cfg).resolve_package_config(NeedsProdDb)
        assert result.cdm_db == "cdm_db"


class TestRefToAbstractDatabaseConfigRejected:
    """DatabaseConfig is abstract (kind has no default) and must not be a
    constructible RefTo target: a field typed against it, not a concrete
    subclass, would let the CLI wizard build a bare DatabaseConfig instead
    of a CDMDatabaseConfig/GenericDatabaseConfig."""

    def test_raises_unknown_ref_target(self):
        class BadConfig(PackageConfigBase):
            tool_name: ClassVar[str] = "bad_tool"
            cdm_db: Annotated[str, RefTo(DatabaseConfig)] = "cdm_db"

        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"cdm_db": CDMDatabaseConfig(connection="db")},
        )
        with pytest.raises(UnknownRefTarget) as exc_info:
            Resolver(cfg).resolve_package_config(BadConfig)
        msg = str(exc_info.value)
        assert "cdm_db" in msg
        assert "CDMDatabaseConfig" in msg
        assert "GenericDatabaseConfig" in msg
