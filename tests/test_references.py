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
    clone_entry,
    delete_entry,
    find_references,
    references_to,
    remove_tool_section,
    rename_entry,
)
from oa_configurator.references import (
    Referenceable,
    Sectioned,
    UnknownRefTarget,
    _ref_section,
    _referenceable_classes,
    collect_references,
    discover_package_configs,
)
from oa_configurator.stack_config import _entry_classes


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


class TestReferenceable:
    def test_entry_sections_are_the_fields_holding_entries(self):
        assert StackConfig.entry_sections() == (
            "connections",
            "databases",
            "providers",
            "models",
            "vector_stores",
        )

    def test_every_class_section_matches_the_field_that_holds_it(self):
        sections = {c.section for c in _referenceable_classes()}

        assert sections == set(StackConfig.entry_sections())

    @pytest.mark.parametrize("field", StackConfig.entry_sections())
    def test_classes_in_a_field_declare_that_fields_name_as_their_section(self, field):
        classes = _entry_classes(StackConfig.model_fields[field].annotation)

        assert classes
        assert {c.section for c in classes} == {field}

    def test_ref_section_reads_the_class_attribute(self):
        assert _ref_section(ConnectionConfig) == "connections"
        assert _ref_section(CDMDatabaseConfig) == "databases"
        assert _ref_section(GenericDatabaseConfig) == "databases"
        assert _ref_section(VectorStoreConfig) == "vector_stores"

    def test_abstract_database_base_is_sectioned_but_not_referenceable(self):
        from oa_configurator.domains.resources.schema import DatabaseConfig

        assert issubclass(DatabaseConfig, Sectioned)
        assert not issubclass(DatabaseConfig, Referenceable)
        with pytest.raises(UnknownRefTarget, match="CDMDatabaseConfig"):
            _ref_section(DatabaseConfig, field_name="cdm_db")

    def test_section_is_not_part_of_the_serialised_entry(self):
        dumped = _conn().model_dump()

        assert "section" not in dumped
        assert "section" not in ConnectionConfig.model_json_schema()["properties"]


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


_CANARY = "edit-canary-pw"


def _pg(database_name: str = "d", **kwargs) -> ConnectionConfig:
    return ConnectionConfig(
        dialect="postgresql+psycopg",
        host="h",
        port=5432,
        user="u",
        password=_CANARY,
        database_name=database_name,
        **kwargs,
    )


def _pg_stack(**kwargs) -> StackConfig:
    base = {
        "connections": {"prod": _pg()},
        "databases": {"cdm_db": CDMDatabaseConfig(connection="prod", cdm_schema="cdm")},
        "providers": {"p": ProviderConfig(provider="ollama", api_key=_CANARY)},
        "models": {"m": ModelConfig(provider="p", model="x")},
    }
    return StackConfig.for_session(**{**base, **kwargs})


class TestCloneEntry:
    def test_provider_clone_without_overrides_is_appended_and_source_kept(self):
        cfg = _pg_stack()

        planned = clone_entry(cfg, "providers", "p", "p2")

        assert list(planned.providers) == ["p", "p2"]
        assert planned.providers["p2"] == planned.providers["p"]
        assert planned.providers["p2"].api_key == _CANARY
        assert list(cfg.providers) == ["p"]

    def test_clone_keeps_the_loaded_path(self, tmp_path):
        cfg = _pg_stack()
        cfg.bind_loaded_path(tmp_path / "config.toml")

        planned = clone_entry(cfg, "providers", "p", "p2")

        assert planned.loaded_path == cfg.loaded_path

    def test_nothing_is_repointed_to_the_copy(self):
        cfg = _pg_stack(tools={"db_tool": {"cdm_db": "cdm_db"}})

        planned = clone_entry(
            cfg, "connections", "prod", "other", overrides={"database_name": "d2"}
        )

        assert planned.databases["cdm_db"].connection == "prod"
        assert planned.tools["db_tool"] == {"cdm_db": "cdm_db"}
        assert planned.connections["other"].database_name == "d2"

    def test_database_clone_unchanged_is_refused_by_validation(self):
        cfg = _pg_stack()

        with pytest.raises(ConfigurationError, match="same physical connection"):
            clone_entry(cfg, "databases", "cdm_db", "cdm_copy")

    def test_test_database_use_case_is_two_pure_calls(self):
        cfg = _pg_stack()

        with_connection = clone_entry(
            cfg,
            "connections",
            "prod",
            "test",
            overrides={"database_name": "d_test", "test_only": True},
        )
        planned = clone_entry(
            with_connection, "databases", "cdm_db", "cdm_test", overrides={"connection": "test"}
        )

        assert planned.databases["cdm_test"].connection == "test"
        assert planned.databases["cdm_test"].cdm_schema == "cdm"
        assert planned.databases["cdm_db"].connection == "prod"
        assert planned.connections["test"].password == _CANARY
        assert _CANARY not in repr(planned)

    def test_connection_clone_unchanged_is_refused_without_leaking_the_password(self):
        with pytest.raises(ConfigurationError) as exc_info:
            clone_entry(_pg_stack(), "connections", "prod", "prod2")

        assert "same connection" in str(exc_info.value)
        assert _CANARY not in str(exc_info.value)

    def test_unknown_override_is_refused_without_the_value(self):
        with pytest.raises(ConfigurationError) as exc_info:
            clone_entry(_pg_stack(), "providers", "p", "p2", overrides={"nope": _CANARY})

        assert "nope" in str(exc_info.value)
        assert _CANARY not in str(exc_info.value)

    def test_bad_override_value_is_refused_without_the_value(self):
        with pytest.raises(ConfigurationError) as exc_info:
            clone_entry(_pg_stack(), "connections", "prod", "c2", overrides={"port": _CANARY})

        assert "port" in str(exc_info.value)
        assert _CANARY not in str(exc_info.value)

    def test_database_kind_cannot_be_overridden(self):
        with pytest.raises(ConfigurationError, match="kind"):
            clone_entry(_pg_stack(), "databases", "cdm_db", "other", overrides={"kind": "generic"})

    def test_override_pointing_at_a_missing_target_is_refused(self):
        with pytest.raises(ConfigurationError, match="unknown connection"):
            clone_entry(
                _pg_stack(), "databases", "cdm_db", "other", overrides={"connection": "ghost"}
            )

    @pytest.mark.parametrize(
        ("section", "source", "new", "message"),
        [
            ("tools", "p", "p2", "Cannot clone in"),
            ("providers", "missing", "p2", "No provider named 'missing'"),
            ("providers", "p", "p", "already exists"),
            ("providers", "p", "", "not a valid provider name"),
            ("providers", "p", " p2", "not a valid provider name"),
        ],
    )
    def test_refusals(self, section, source, new, message):
        with pytest.raises(ConfigurationError, match=message):
            clone_entry(_pg_stack(), section, source, new)


class TestDeleteEntry:
    def test_unreferenced_entry_is_removed_and_input_kept(self):
        cfg = _pg_stack(
            providers={
                "p": ProviderConfig(provider="ollama"),
                "spare": ProviderConfig(provider="ollama"),
            }
        )

        planned = delete_entry(cfg, "providers", "spare", package_configs=REGISTRY)

        assert list(planned.providers) == ["p"]
        assert list(cfg.providers) == ["p", "spare"]

    def test_delete_keeps_the_loaded_path(self, tmp_path):
        cfg = _pg_stack(
            providers={
                "p": ProviderConfig(provider="ollama"),
                "spare": ProviderConfig(provider="ollama"),
            }
        )
        cfg.bind_loaded_path(tmp_path / "config.toml")

        planned = delete_entry(cfg, "providers", "spare", package_configs=REGISTRY)

        assert planned.loaded_path == cfg.loaded_path

    def test_referenced_entry_is_refused_and_references_are_listed(self):
        with pytest.raises(ConfigurationError) as exc_info:
            delete_entry(_pg_stack(), "connections", "prod", package_configs=REGISTRY)

        assert "databases.cdm_db.connection" in str(exc_info.value)

    def test_tool_field_with_an_explicit_value_blocks_delete(self):
        cfg = _pg_stack(tools={"db_tool": {"cdm_db": "cdm_db"}})

        with pytest.raises(ConfigurationError, match="tools.db_tool.cdm_db"):
            delete_entry(cfg, "databases", "cdm_db", package_configs=REGISTRY)

    def test_tool_field_resolving_through_its_default_blocks_delete(self):
        cfg = _pg_stack(tools={"db_tool": {}})

        with pytest.raises(ConfigurationError, match="tools.db_tool.cdm_db"):
            delete_entry(cfg, "databases", "cdm_db", package_configs=REGISTRY)

    def test_tool_field_pointing_elsewhere_does_not_block_delete(self):
        cfg = _pg_stack(
            databases={
                "cdm_db": CDMDatabaseConfig(connection="prod"),
                "other": GenericDatabaseConfig(connection="prod"),
            },
            tools={"db_tool": {"cdm_db": "other"}},
        )

        planned = delete_entry(cfg, "databases", "cdm_db", package_configs=REGISTRY)

        assert list(planned.databases) == ["other"]

    def test_unknown_tool_schema_blocks_delete(self):
        cfg = _pg_stack(
            providers={
                "p": ProviderConfig(provider="ollama"),
                "spare": ProviderConfig(provider="ollama"),
            },
            tools={"mystery": {"x": 1}},
        )

        with pytest.raises(ConfigurationError, match=r"Cannot delete safely.*\[tools.mystery\]"):
            delete_entry(cfg, "providers", "spare", package_configs=REGISTRY)

    @pytest.mark.parametrize(
        ("section", "name", "message"),
        [
            ("tools", "db_tool", "Cannot delete in"),
            ("providers", "missing", "No provider named 'missing'"),
        ],
    )
    def test_refusals(self, section, name, message):
        with pytest.raises(ConfigurationError, match=message):
            delete_entry(_pg_stack(), section, name, package_configs=REGISTRY)


class TestRemoveToolSection:
    def test_section_is_removed_and_others_and_input_are_kept(self):
        cfg = _pg_stack(tools={"db_tool": {"cdm_db": "cdm_db"}, "mystery": {"x": 1}})

        planned = remove_tool_section(cfg, "mystery")

        assert planned.tools == {"db_tool": {"cdm_db": "cdm_db"}}
        assert set(cfg.tools) == {"db_tool", "mystery"}

    def test_removal_unblocks_a_delete_that_an_unknown_section_prevented(self):
        cfg = _pg_stack(
            providers={
                "p": ProviderConfig(provider="ollama"),
                "spare": ProviderConfig(provider="ollama"),
            },
            tools={"stale_pkg": {"x": 1}},
        )

        cleaned = remove_tool_section(cfg, "stale_pkg")
        planned = delete_entry(cleaned, "providers", "spare", package_configs=REGISTRY)

        assert list(planned.providers) == ["p"]

    def test_missing_section_is_refused(self):
        with pytest.raises(ConfigurationError, match="No tool section named 'nope'"):
            remove_tool_section(_pg_stack(), "nope")
