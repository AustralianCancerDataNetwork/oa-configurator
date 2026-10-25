"""Tests for references.py: reference lookup and rename."""

from __future__ import annotations

from typing import Annotated, ClassVar

import pytest
from pydantic import field_validator

from oa_configurator import (
    CDMDatabaseConfig,
    ConfigurationError,
    ConnectionConfig,
    Dialect,
    GenericDatabaseConfig,
    ModelConfig,
    PackageConfigBase,
    PackageConfigInvalidError,
    ProviderConfig,
    Reference,
    RefTo,
    StackConfig,
    VectorStoreConfig,
    find_references,
    references_to,
    rename_entry,
)
from oa_configurator.references import collect_references, discover_package_configs


class DbTool(PackageConfigBase):
    tool_name: ClassVar[str] = "db_tool"
    cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"
    model_name: Annotated[str | None, RefTo(ModelConfig)] = None


class OtherTool(PackageConfigBase):
    tool_name: ClassVar[str] = "other_tool"
    port: int = 1


REGISTRY: dict[str, type[PackageConfigBase]] = {"db_tool": DbTool, "other_tool": OtherTool}


def _conn() -> ConnectionConfig:
    return ConnectionConfig(dialect=Dialect.SQLITE, database_name=":memory:")


def _stack(**kwargs) -> StackConfig:
    return StackConfig.for_session(
        connections={"c1": _conn(), "c2": _conn(), "unused": _conn()},
        providers={"p": ProviderConfig(provider="ollama")},
        **kwargs,
    )


class TestEntryReferences:
    def test_primary_and_vocab_connection_are_both_found(self):
        cfg = _stack(
            databases={
                "cdm_db": CDMDatabaseConfig(connection="c1", vocab_connection="c2"),
                "other": GenericDatabaseConfig(connection="c1"),
            }
        )

        report = references_to(cfg, "connections", "c1", package_configs=REGISTRY)

        assert report.references == (
            Reference("databases", "cdm_db", "connection", "connections", "c1"),
            Reference("databases", "other", "connection", "connections", "c1"),
        )
        vocab = references_to(cfg, "connections", "c2", package_configs=REGISTRY)
        assert [r.field for r in vocab.references] == ["vocab_connection"]

    def test_model_to_provider_and_vector_store_to_database(self):
        cfg = _stack(
            databases={"vec_db": GenericDatabaseConfig(connection="c1")},
            models={"m": ModelConfig(provider="p", model="x")},
            vector_stores={"vs": VectorStoreConfig(backend_type="x", database="vec_db")},
        )

        providers = references_to(cfg, "providers", "p", package_configs=REGISTRY)
        databases = references_to(cfg, "databases", "vec_db", package_configs=REGISTRY)

        assert providers.references == (Reference("models", "m", "provider", "providers", "p"),)
        assert databases.references == (
            Reference("vector_stores", "vs", "database", "databases", "vec_db"),
        )

    def test_unreferenced_entry_has_no_references(self):
        cfg = _stack(databases={"cdm_db": CDMDatabaseConfig(connection="c1")})

        report = references_to(cfg, "connections", "unused", package_configs=REGISTRY)

        assert report.references == ()
        assert report.unknown_tool_schemas == ()

    def test_same_name_in_another_section_is_not_a_reference(self):
        cfg = StackConfig.for_session(
            connections={"x": _conn()},
            providers={"x": ProviderConfig(provider="ollama")},
            models={"m": ModelConfig(provider="x", model="y")},
        )

        report = references_to(cfg, "connections", "x", package_configs=REGISTRY)

        assert report.references == ()

    def test_section_that_cannot_be_referenced_is_rejected(self):
        with pytest.raises(ValueError, match="tools"):
            references_to(StackConfig.for_session(), "tools", "x", package_configs=REGISTRY)

    def test_order_is_deterministic(self):
        cfg = _stack(
            databases={
                "z": GenericDatabaseConfig(connection="c1"),
                "a": GenericDatabaseConfig(connection="c1"),
            }
        )

        report = references_to(cfg, "connections", "c1", package_configs=REGISTRY)

        assert [r.source_name for r in report.references] == ["a", "z"]


class TestCollectReferences:
    def test_lists_entry_and_tool_references_in_one_report(self):
        cfg = _stack(
            databases={"cdm_db": CDMDatabaseConfig(connection="c1", vocab_connection="c2")},
            tools={"db_tool": {"model_name": "m"}, "mystery": {}},
        )

        report = collect_references(cfg, package_configs=REGISTRY)

        assert report.references == (
            Reference("databases", "cdm_db", "connection", "connections", "c1"),
            Reference("databases", "cdm_db", "vocab_connection", "connections", "c2"),
            Reference("tools", "db_tool", "cdm_db", "databases", "cdm_db"),
            Reference("tools", "db_tool", "model_name", "models", "m"),
        )
        assert report.unknown_tool_schemas == ("mystery",)

    def test_references_to_is_a_filter_of_the_full_list(self):
        cfg = _stack(
            databases={"cdm_db": CDMDatabaseConfig(connection="c1", vocab_connection="c2")},
            tools={"db_tool": {}},
        )

        full = collect_references(cfg, package_configs=REGISTRY)
        filtered = references_to(cfg, "connections", "c2", package_configs=REGISTRY)

        assert filtered.references == tuple(
            r for r in full.references if (r.target_section, r.target_name) == ("connections", "c2")
        )

    def test_non_string_tool_value_is_not_a_reference(self):
        cfg = _stack(tools={"db_tool": {"cdm_db": 5}})

        report = collect_references(cfg, package_configs=REGISTRY)

        assert report.references == ()


def test_forward_helpers_remain_importable_from_stack_config():
    from oa_configurator import references, stack_config

    assert stack_config.unresolved_refs is references.unresolved_refs
    assert stack_config.mismatched_kind_refs is references.mismatched_kind_refs
    assert stack_config.UnknownRefTarget is references.UnknownRefTarget


class TestToolReferences:
    def test_explicit_field_value_is_found(self):
        cfg = _stack(
            databases={"main": CDMDatabaseConfig(connection="c1")},
            tools={"db_tool": {"cdm_db": "main"}},
        )

        report = references_to(cfg, "databases", "main", package_configs=REGISTRY)

        assert report.references == (Reference("tools", "db_tool", "cdm_db", "databases", "main"),)

    def test_omitted_field_resolving_to_its_default_is_found(self):
        cfg = _stack(
            databases={"cdm_db": CDMDatabaseConfig(connection="c1")},
            tools={"db_tool": {}},
        )

        report = references_to(cfg, "databases", "cdm_db", package_configs=REGISTRY)

        assert report.references == (
            Reference("tools", "db_tool", "cdm_db", "databases", "cdm_db"),
        )

    def test_explicit_value_overrides_default(self):
        cfg = _stack(
            databases={
                "cdm_db": CDMDatabaseConfig(connection="c1"),
                "main": CDMDatabaseConfig(connection="c1"),
            },
            tools={"db_tool": {"cdm_db": "main"}},
        )

        assert references_to(cfg, "databases", "cdm_db", package_configs=REGISTRY).references == ()

    def test_none_default_is_not_a_reference(self):
        cfg = _stack(tools={"db_tool": {}})

        assert references_to(cfg, "models", "None", package_configs=REGISTRY).references == ()

    def test_tool_without_ref_fields_is_known_and_empty(self):
        cfg = _stack(tools={"other_tool": {"port": 2}})

        report = references_to(cfg, "connections", "c1", package_configs=REGISTRY)

        assert report.references == ()
        assert report.unknown_tool_schemas == ()

    def test_unknown_tool_section_is_reported_not_ignored(self):
        cfg = _stack(tools={"mystery": {"db": "c1"}, "db_tool": {}})

        report = references_to(cfg, "connections", "c1", package_configs=REGISTRY)

        assert report.unknown_tool_schemas == ("mystery",)

    def test_registered_package_absent_from_stack_is_not_unknown(self):
        report = references_to(_stack(), "connections", "c1", package_configs=REGISTRY)

        assert report.unknown_tool_schemas == ()


class _FakeEP:
    def __init__(self, name, target=None, error=None):
        self.name = name
        self._target = target
        self._error = error

    def load(self):
        if self._error is not None:
            raise self._error
        return self._target


class DuplicateDbTool(PackageConfigBase):
    tool_name: ClassVar[str] = "db_tool"


@pytest.fixture
def fake_entry_points(monkeypatch):
    def install(*eps):
        monkeypatch.setattr("oa_configurator.references.package_entry_points", lambda: list(eps))

    return install


class TestDiscovery:
    def test_usable_classes_are_keyed_by_tool_name(self, fake_entry_points):
        fake_entry_points(_FakeEP("a", DbTool), _FakeEP("b", OtherTool))

        configs, problems = discover_package_configs()

        assert configs == REGISTRY
        assert problems == ()

    def test_same_class_registered_twice_is_not_a_duplicate(self, fake_entry_points):
        fake_entry_points(_FakeEP("a", DbTool), _FakeEP("a2", DbTool))

        configs, problems = discover_package_configs()

        assert configs == {"db_tool": DbTool}
        assert problems == ()

    def test_conflicting_classes_are_dropped_and_reported(self, fake_entry_points):
        fake_entry_points(_FakeEP("a", DbTool), _FakeEP("b", DuplicateDbTool))

        configs, problems = discover_package_configs()

        assert "db_tool" not in configs
        (problem,) = problems
        assert (problem.kind, problem.name) == ("duplicate_name", "db_tool")

    def test_broken_import_is_reported_without_the_message(self, fake_entry_points):
        fake_entry_points(_FakeEP("broken", error=ImportError("secret-detail")))

        configs, problems = discover_package_configs()

        assert configs == {}
        (problem,) = problems
        assert (problem.kind, problem.name, problem.detail) == (
            "broken_import",
            "broken",
            "ImportError",
        )

    def test_non_package_target_is_reported(self, fake_entry_points):
        fake_entry_points(_FakeEP("odd", int))

        configs, problems = discover_package_configs()

        assert configs == {}
        assert problems[0].kind == "not_a_package_config"

    def test_find_references_marks_unusable_packages_as_unknown(self, fake_entry_points):
        fake_entry_points(_FakeEP("db_tool", DbTool), _FakeEP("other_tool", error=ImportError()))
        cfg = _stack(
            databases={"cdm_db": CDMDatabaseConfig(connection="c1")},
            tools={"db_tool": {}, "other_tool": {}},
        )

        report = find_references(cfg, "databases", "cdm_db")

        assert [r.source_name for r in report.references] == ["db_tool"]
        assert report.unknown_tool_schemas == ("other_tool",)
        assert [p.kind for p in report.discovery_problems] == ["broken_import"]


class RenameDbTool(PackageConfigBase):
    tool_name: ClassVar[str] = "db_tool"
    cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"


class PickyTool(PackageConfigBase):
    tool_name: ClassVar[str] = "picky_tool"
    cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"

    @field_validator("cdm_db")
    @classmethod
    def _no_renamed(cls, value: str) -> str:
        if value.startswith("bad"):
            raise ValueError("rejected name")
        return value


RENAME_REGISTRY: dict[str, type[PackageConfigBase]] = {
    "db_tool": RenameDbTool,
    "picky_tool": PickyTool,
}


def _rename_stack(**kwargs) -> StackConfig:
    base = {
        "connections": {"first": _conn(), "c1": _conn(), "last": _conn()},
        "databases": {"cdm_db": CDMDatabaseConfig(connection="c1", vocab_connection="last")},
        "providers": {"p": ProviderConfig(provider="ollama")},
        "models": {"m": ModelConfig(provider="p", model="x")},
    }
    return StackConfig.for_session(**{**base, **kwargs})


class TestRenameEntry:
    def test_entry_and_all_core_references_are_repointed(self):
        cfg = _rename_stack(
            databases={
                "cdm_db": CDMDatabaseConfig(connection="c1", vocab_connection="last"),
                "generic": GenericDatabaseConfig(connection="c1"),
            }
        )

        planned = rename_entry(cfg, "connections", "c1", "renamed", package_configs=RENAME_REGISTRY)

        assert "c1" not in planned.connections
        assert "renamed" in planned.connections
        assert planned.databases["cdm_db"].connection == "renamed"
        assert planned.databases["generic"].connection == "renamed"
        assert planned.databases["cdm_db"].vocab_connection == "last"

    def test_entry_keeps_its_position(self):
        planned = rename_entry(
            _rename_stack(), "connections", "c1", "renamed", package_configs=RENAME_REGISTRY
        )

        assert list(planned.connections) == ["first", "renamed", "last"]

    def test_model_provider_and_vector_store_references(self):
        cfg = _rename_stack(
            databases={"vec": GenericDatabaseConfig(connection="c1")},
            vector_stores={"vs": VectorStoreConfig(backend_type="x", database="vec")},
        )

        by_provider = rename_entry(cfg, "providers", "p", "p2", package_configs=RENAME_REGISTRY)
        by_database = rename_entry(cfg, "databases", "vec", "vec2", package_configs=RENAME_REGISTRY)

        assert by_provider.models["m"].provider == "p2"
        assert by_database.vector_stores["vs"].database == "vec2"

    def test_explicit_tool_value_is_repointed(self):
        cfg = _rename_stack(tools={"db_tool": {"cdm_db": "cdm_db"}})

        planned = rename_entry(cfg, "databases", "cdm_db", "main", package_configs=RENAME_REGISTRY)

        assert planned.tools["db_tool"]["cdm_db"] == "main"

    def test_defaulted_tool_value_becomes_explicit(self):
        cfg = _rename_stack(tools={"db_tool": {}})

        planned = rename_entry(cfg, "databases", "cdm_db", "main", package_configs=RENAME_REGISTRY)

        assert planned.tools["db_tool"] == {"cdm_db": "main"}

    def test_unrelated_tool_section_is_untouched(self):
        cfg = _rename_stack(
            databases={
                "cdm_db": CDMDatabaseConfig(connection="c1"),
                "main": CDMDatabaseConfig(connection="c1"),
            },
            tools={"db_tool": {"cdm_db": "main"}},
        )

        planned = rename_entry(cfg, "databases", "cdm_db", "other", package_configs=RENAME_REGISTRY)

        assert planned.tools == cfg.tools

    def test_input_is_not_mutated_and_provenance_is_kept(self, tmp_path):
        cfg = _rename_stack(tools={"db_tool": {"cdm_db": "cdm_db"}})
        cfg.bind_loaded_path(tmp_path / "config.toml")
        before = cfg.model_dump(mode="python")

        planned = rename_entry(cfg, "databases", "cdm_db", "main", package_configs=RENAME_REGISTRY)

        assert cfg.model_dump(mode="python") == before
        assert planned is not cfg
        assert planned.loaded_path == cfg.loaded_path

    def test_no_file_io(self, monkeypatch):
        def unexpected_io(*args, **kwargs):
            raise AssertionError("rename_entry must not touch the filesystem")

        monkeypatch.setattr("oa_configurator.loader.load_stack_config", unexpected_io)
        monkeypatch.setattr("oa_configurator.io.save_stack_config", unexpected_io)

        rename_entry(
            _rename_stack(), "connections", "c1", "renamed", package_configs=RENAME_REGISTRY
        )


class TestRenameRefusals:
    def test_unknown_tool_schema_refuses(self):
        cfg = _rename_stack(tools={"mystery": {"db": "c1"}})

        with pytest.raises(ConfigurationError, match=r"\[tools.mystery\]"):
            rename_entry(cfg, "connections", "c1", "renamed", package_configs=RENAME_REGISTRY)

    def test_target_name_collision_refuses(self):
        with pytest.raises(ConfigurationError, match="already exists"):
            rename_entry(
                _rename_stack(), "connections", "c1", "last", package_configs=RENAME_REGISTRY
            )

    def test_database_kinds_share_one_namespace(self):
        cfg = _rename_stack(
            databases={
                "cdm_db": CDMDatabaseConfig(connection="c1"),
                "generic": GenericDatabaseConfig(connection="c1"),
            }
        )

        with pytest.raises(ConfigurationError, match="already exists"):
            rename_entry(cfg, "databases", "cdm_db", "generic", package_configs=RENAME_REGISTRY)

    def test_missing_entry_refuses(self):
        with pytest.raises(ConfigurationError, match="No connection named"):
            rename_entry(
                _rename_stack(), "connections", "nope", "x", package_configs=RENAME_REGISTRY
            )

    @pytest.mark.parametrize("new", ["", "  ", " padded ", "c1"])
    def test_invalid_or_unchanged_name_refuses(self, new):
        with pytest.raises(ConfigurationError):
            rename_entry(_rename_stack(), "connections", "c1", new, package_configs=RENAME_REGISTRY)

    @pytest.mark.parametrize("section", ["tools", "logging", "nonsense"])
    def test_non_entry_section_refuses(self, section):
        with pytest.raises(ConfigurationError, match="Cannot rename in"):
            rename_entry(_rename_stack(), section, "x", "y", package_configs=RENAME_REGISTRY)

    def test_repointed_package_section_failing_its_schema_raises_without_output(self):
        cfg = _rename_stack(tools={"picky_tool": {"cdm_db": "cdm_db"}})
        before = cfg.model_dump(mode="python")

        with pytest.raises(PackageConfigInvalidError):
            rename_entry(cfg, "databases", "cdm_db", "bad_name", package_configs=RENAME_REGISTRY)

        assert cfg.model_dump(mode="python") == before
