"""Tests for package_base.py: PackageConfigBase factory interface."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Self

import pytest
import typer
from pydantic import BaseModel, Field, model_validator

from oa_configurator import (
    CDMDatabaseConfig,
    ConfigurationError,
    ConnectionConfig,
    DatabaseConfig,
    GenericDatabaseConfig,
    ModelConfig,
    PackageConfigBase,
    PackageConfigValidationError,
    ProviderConfig,
    RefTo,
    Resolver,
    StackConfig,
    UnknownRefTarget,
    VectorStoreConfig,
    mismatched_kind_refs,
    plan_configure,
    unresolved_refs,
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
        cfg = StackConfig.for_session(tools={"sample_tool": {}})
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
        cfg = StackConfig.for_session(tools={"sample_tool": extra})
        restored = Resolver(cfg).resolve_package_config(SampleConfig)
        assert restored.backend == "sqlitevec"
        assert restored.file_path == "/embeddings"

    def test_subclass_must_set_tool_name(self):
        with pytest.raises((AttributeError, TypeError)):

            class BadConfig(PackageConfigBase):
                pass

            # Accessing tool_name on an instance should fail
            BadConfig().tool_name  # type: ignore[attr-defined]


class NestedLimits(BaseModel):
    batch_size: int = Field(ge=1, le=100)


class ValidatedPackageConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "validated_tool"
    cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"
    port: int = Field(default=8000, ge=1, le=65535)
    workers: int = Field(default=1, ge=1)
    max_workers: int = Field(default=4, ge=1)
    limits: NestedLimits = Field(default_factory=lambda: NestedLimits(batch_size=10))

    @model_validator(mode="after")
    def workers_fit_limit(self) -> Self:
        if self.workers > self.max_workers:
            raise ValueError("workers must not exceed max_workers")
        return self


def _validated_stack(tool_values: dict[str, Any]) -> StackConfig:
    return StackConfig.for_session(
        connections={
            "db": ConnectionConfig(dialect="sqlite", database_name=":memory:")
        },
        databases={"cdm_db": CDMDatabaseConfig(connection="db")},
        tools={"validated_tool": tool_values},
    )


class TestPackageCandidateValidation:
    def test_scalar_constraint_identifies_package_and_field(self):
        cfg = _validated_stack({"cdm_db": "cdm_db", "port": 999999})

        with pytest.raises(PackageConfigValidationError) as exc_info:
            ValidatedPackageConfig.validate_candidate(cfg)

        assert exc_info.value.tool_name == "validated_tool"
        assert "[tools.validated_tool]" in str(exc_info.value)
        assert exc_info.value.errors()[0]["loc"] == ("port",)

    def test_nested_validation_location_is_preserved(self):
        cfg = _validated_stack({"cdm_db": "cdm_db", "limits": {"batch_size": 1000}})

        with pytest.raises(PackageConfigValidationError) as exc_info:
            ValidatedPackageConfig.validate_candidate(cfg)

        assert exc_info.value.errors()[0]["loc"] == ("limits", "batch_size")

    def test_cross_field_error_retains_model_location(self):
        cfg = _validated_stack({"cdm_db": "cdm_db", "workers": 8, "max_workers": 4})

        with pytest.raises(PackageConfigValidationError) as exc_info:
            ValidatedPackageConfig.validate_candidate(cfg)

        assert exc_info.value.errors()[0]["loc"] == ()
        assert "workers must not exceed max_workers" in str(exc_info.value)

    def test_missing_reference_identifies_package_field(self):
        cfg = _validated_stack({"cdm_db": "missing"})

        with pytest.raises(
            ConfigurationError, match=r"\[tools\.validated_tool\]\.cdm_db"
        ):
            ValidatedPackageConfig.validate_candidate(cfg)

    def test_wrong_reference_kind_identifies_package_field(self):
        cfg = StackConfig.for_session(
            connections={
                "db": ConnectionConfig(dialect="sqlite", database_name=":memory:")
            },
            databases={"generic": GenericDatabaseConfig(connection="db")},
            tools={"validated_tool": {"cdm_db": "generic"}},
        )

        with pytest.raises(ConfigurationError, match="requires a CDMDatabaseConfig"):
            ValidatedPackageConfig.validate_candidate(cfg)

    def test_valid_candidate_returns_normalized_package_model(self):
        cfg = _validated_stack({"cdm_db": "cdm_db", "port": "9000"})

        result = ValidatedPackageConfig.validate_candidate(cfg)

        assert result.port == 9000
        assert isinstance(result.port, int)


class TestPlanConfigure:
    def test_returns_new_validated_candidate_without_file_io(
        self, tmp_path, monkeypatch
    ):
        cfg = _validated_stack({"cdm_db": "cdm_db", "port": 8000})
        cfg.bind_loaded_path(tmp_path / "config.toml")
        before = cfg.model_dump(mode="python")

        def unexpected_io(*args, **kwargs):
            raise AssertionError("planner performed file I/O")

        monkeypatch.setattr("oa_configurator.loader.load_stack_config", unexpected_io)
        monkeypatch.setattr("oa_configurator.io.save_stack_config", unexpected_io)

        planned = plan_configure(ValidatedPackageConfig, cfg, {"port": 9000})

        assert planned is not cfg
        assert planned.tools["validated_tool"]["port"] == 9000
        assert cfg.model_dump(mode="python") == before
        assert cfg.tools["validated_tool"]["port"] == 8000
        assert planned.loaded_path == cfg.loaded_path

    def test_nested_refto_creation_is_confined_to_returned_candidate(self):
        cfg = StackConfig.for_session()

        planned = plan_configure(
            ValidatedPackageConfig,
            cfg,
            {
                "cdm_db": {
                    "connection": {
                        "dialect": "sqlite",
                        "database_name": ":memory:",
                    },
                    "schema_name": "planned_omop",
                }
            },
        )

        database_name = planned.tools["validated_tool"]["cdm_db"]
        database = planned.databases[database_name]
        assert database.schema_name == "planned_omop"
        assert database.connection in planned.connections
        assert cfg.connections == {}
        assert cfg.databases == {}
        assert cfg.tools == {}

    def test_nested_refto_update_carries_over_unmentioned_target_fields(self):
        cfg = _validated_stack({"cdm_db": "cdm_db"})
        cfg.databases["cdm_db"].schema_name = "original_schema"

        planned = plan_configure(
            ValidatedPackageConfig,
            cfg,
            {"cdm_db": {"name": "cdm_db", "schema_name": "planned_schema"}},
        )

        assert planned.databases["cdm_db"].schema_name == "planned_schema"
        assert planned.databases["cdm_db"].connection == "db"
        assert cfg.databases["cdm_db"].schema_name == "original_schema"

    def test_stored_package_values_carry_over(self):
        cfg = _validated_stack(
            {
                "cdm_db": "cdm_db",
                "port": 9000,
                "workers": 1,
                "max_workers": 4,
            }
        )

        planned = plan_configure(ValidatedPackageConfig, cfg, {"workers": 2})

        assert planned.tools["validated_tool"]["port"] == 9000
        assert planned.tools["validated_tool"]["workers"] == 2
        assert planned.tools["validated_tool"]["max_workers"] == 4

    def test_validation_failure_leaves_input_unchanged(self):
        cfg = _validated_stack({"cdm_db": "cdm_db", "port": 8000})
        before = cfg.model_dump(mode="python")

        with pytest.raises(PackageConfigValidationError):
            plan_configure(ValidatedPackageConfig, cfg, {"port": 999999})

        assert cfg.model_dump(mode="python") == before

    def test_public_reference_helpers_are_importable(self):
        assert callable(unresolved_refs)
        assert callable(mismatched_kind_refs)


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
            connections={
                "db": ConnectionConfig(dialect="sqlite", database_name=":memory:")
            },
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
            models={
                "embed-default": ModelConfig(provider="p", model="nomic-embed-text")
            },
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
            connections={
                "db": ConnectionConfig(dialect="sqlite", database_name=":memory:")
            },
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
            playground_db: Annotated[
                str | None, RefTo(CDMDatabaseConfig, is_test=True)
            ] = None
            test_flavoured_prod_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "prod_db"

        fields = dict(OddlyNamedConfig.model_fields)
        playground_ref = next(
            m for m in fields["playground_db"].metadata if isinstance(m, RefTo)
        )
        prod_ref = next(
            m for m in fields["test_flavoured_prod_db"].metadata if isinstance(m, RefTo)
        )
        assert playground_ref.is_test is True
        assert prod_ref.is_test is False


class TestIsTestOnlyMatchEnforcement:
    """resolve_package_config checks that a field's is_test declaration
    agrees with the test_only flag on whatever connection it resolves to,
    in both directions."""

    def test_test_field_pointed_at_real_connection_raises(self):
        cfg = StackConfig.for_session(
            connections={
                "prod": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=False
                )
            },
            databases={"test_cdm_db": CDMDatabaseConfig(connection="prod")},
        )

        class NeedsTestDb(PackageConfigBase):
            tool_name: ClassVar[str] = "needs_test_db_tool"
            test_cdm_db: Annotated[
                str | None, RefTo(CDMDatabaseConfig, is_test=True)
            ] = "test_cdm_db"

        with pytest.raises(ConfigurationError, match="is_test=True"):
            Resolver(cfg).resolve_package_config(NeedsTestDb)

    def test_prod_field_pointed_at_test_only_connection_raises(self):
        cfg = StackConfig.for_session(
            connections={
                "test_conn": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=True
                )
            },
            databases={"cdm_db": CDMDatabaseConfig(connection="test_conn")},
        )

        class NeedsProdDb(PackageConfigBase):
            tool_name: ClassVar[str] = "needs_prod_db_tool"
            cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"

        with pytest.raises(ConfigurationError, match="is_test=False"):
            Resolver(cfg).resolve_package_config(NeedsProdDb)

    def test_matching_is_test_and_test_only_passes(self):
        cfg = StackConfig.for_session(
            connections={
                "test_conn": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=True
                )
            },
            databases={"test_cdm_db": CDMDatabaseConfig(connection="test_conn")},
        )

        class NeedsTestDb(PackageConfigBase):
            tool_name: ClassVar[str] = "matching_test_db_tool"
            test_cdm_db: Annotated[
                str | None, RefTo(CDMDatabaseConfig, is_test=True)
            ] = "test_cdm_db"

        result = Resolver(cfg).resolve_package_config(NeedsTestDb)
        assert result.test_cdm_db == "test_cdm_db"

    def test_matching_non_test_passes(self):
        cfg = StackConfig.for_session(
            connections={
                "prod": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=False
                )
            },
            databases={"cdm_db": CDMDatabaseConfig(connection="prod")},
        )

        class NeedsProdDb(PackageConfigBase):
            tool_name: ClassVar[str] = "matching_prod_db_tool"
            cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"

        result = Resolver(cfg).resolve_package_config(NeedsProdDb)
        assert result.cdm_db == "cdm_db"

    def test_vocab_only_test_connection_does_not_make_database_test(self):
        """A CDMDatabaseConfig's test-ness is decided by its primary
        connection alone, not vocab_connection: a prod-primary database
        with a test-only vocab connection is NOT test, matching what the
        CLI wizard's candidate filtering (_is_test_marked) and this
        validator both now agree on."""
        cfg = StackConfig.for_session(
            connections={
                "prod": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=False
                ),
                "test_vocab": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=True
                ),
            },
            databases={
                "cdm_db": CDMDatabaseConfig(
                    connection="prod", vocab_connection="test_vocab"
                )
            },
        )

        class NeedsProdDb(PackageConfigBase):
            tool_name: ClassVar[str] = "vocab_edge_case_prod_tool"
            cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"

        class NeedsTestDb(PackageConfigBase):
            tool_name: ClassVar[str] = "vocab_edge_case_test_tool"
            test_cdm_db: Annotated[
                str | None, RefTo(CDMDatabaseConfig, is_test=True)
            ] = "cdm_db"

        result = Resolver(cfg).resolve_package_config(NeedsProdDb)
        assert result.cdm_db == "cdm_db"
        with pytest.raises(ConfigurationError, match="is_test=True"):
            Resolver(cfg).resolve_package_config(NeedsTestDb)

    def test_vector_store_reaching_test_only_database_via_nested_ref(self):
        """A RefTo(VectorStoreConfig, is_test=True) field is checked through
        the VectorStoreConfig -> database -> connection chain, not skipped
        the way a depth-1-only check would skip any non-DatabaseConfig
        RefTo target."""
        cfg = StackConfig.for_session(
            connections={
                "test_conn": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=True
                )
            },
            databases={"emb_db": GenericDatabaseConfig(connection="test_conn")},
            vector_stores={
                "vs": VectorStoreConfig(backend_type="pgvector", database="emb_db")
            },
        )

        class NeedsTestVectorStore(PackageConfigBase):
            tool_name: ClassVar[str] = "vector_store_test_tool"
            vector_store_name: Annotated[
                str | None, RefTo(VectorStoreConfig, is_test=True)
            ] = "vs"

        result = Resolver(cfg).resolve_package_config(NeedsTestVectorStore)
        assert result.vector_store_name == "vs"

    def test_vector_store_reaching_prod_database_with_is_test_true_raises(self):
        cfg = StackConfig.for_session(
            connections={
                "prod_conn": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=False
                )
            },
            databases={"emb_db": GenericDatabaseConfig(connection="prod_conn")},
            vector_stores={
                "vs": VectorStoreConfig(backend_type="pgvector", database="emb_db")
            },
        )

        class NeedsTestVectorStore(PackageConfigBase):
            tool_name: ClassVar[str] = "vector_store_test_tool_2"
            vector_store_name: Annotated[
                str | None, RefTo(VectorStoreConfig, is_test=True)
            ] = "vs"

        with pytest.raises(ConfigurationError, match="is_test=True"):
            Resolver(cfg).resolve_package_config(NeedsTestVectorStore)


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
            connections={
                "db": ConnectionConfig(dialect="sqlite", database_name=":memory:")
            },
            databases={"cdm_db": CDMDatabaseConfig(connection="db")},
        )
        with pytest.raises(UnknownRefTarget) as exc_info:
            Resolver(cfg).resolve_package_config(BadConfig)
        msg = str(exc_info.value)
        assert "cdm_db" in msg
        assert "CDMDatabaseConfig" in msg
        assert "GenericDatabaseConfig" in msg


class BoolFieldConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "bool_field_tool"
    dry_run: bool = False


class RequiredFieldConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "required_field_tool"
    required_value: str


class DictFieldConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "dict_field_tool"
    configuration: dict[str, Any] = {}


class TestResolveFieldsPlainFieldHandling:
    """Plain (non-RefTo) fields get type-aware handling, not a single
    free-text prompt for everything: bool via confirm, dict/list carried
    over or left to their own default -- matching what _resolve_named_entry
    already does for RefTo-target schemas."""

    def test_bool_field_uses_confirm_and_stores_a_real_bool(self, monkeypatch):
        monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)
        extra = BoolFieldConfig.resolve_fields(
            StackConfig.for_session(), set_dict={}, interactive=True
        )
        assert extra["dry_run"] is True

    def test_required_plain_field_has_an_empty_prompt_default(self, monkeypatch):
        seen_defaults: dict[str, str] = {}

        def prompt(text, default="", **kwargs):
            seen_defaults[text] = default
            return "configured"

        monkeypatch.setattr(typer, "prompt", prompt)
        extra = RequiredFieldConfig.resolve_fields(
            StackConfig.for_session(), set_dict={}, interactive=True
        )

        assert seen_defaults["required_value"] == ""
        assert extra["required_value"] == "configured"

    def test_dict_field_is_not_prompted_and_carries_over_stored_value(
        self, monkeypatch
    ):
        cfg = StackConfig.for_session(
            tools={"dict_field_tool": {"configuration": {"k": "v"}}}
        )
        # If this field were free-text prompted, this monkeypatch would be hit and fail the test.
        monkeypatch.setattr(
            typer,
            "prompt",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not prompt")),
        )
        extra = DictFieldConfig.resolve_fields(cfg, set_dict={}, interactive=True)
        assert extra["configuration"] == {"k": "v"}


class MixedFieldConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "mixed_field_tool"
    cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"
    backend: str = "default_backend"


class TestResolveFieldsStaleRefFallback:
    def test_one_dangling_ref_does_not_wipe_other_stored_fields(self):
        """resolve_package_config raises on the dangling cdm_db ref, so the
        except branch's fallback is what resolve_fields actually sees. In that case,
        it must preserve the raw stored section (backend included), not
        reset to an empty dict and lose every other already-configured
        field along with the one broken one."""
        cfg = StackConfig.for_session(
            tools={
                "mixed_field_tool": {
                    "cdm_db": "does-not-exist",
                    "backend": "custom_value",
                }
            },
        )
        extra = MixedFieldConfig.resolve_fields(cfg, set_dict={}, interactive=False)
        assert extra["backend"] == "custom_value"
