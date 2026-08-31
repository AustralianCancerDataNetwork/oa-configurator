"""Tests for loader.py: path normalization, OA_CONFIG_PATH resolution, and
the process-local StackConfig cache keyed on file identity (mtime + size)."""

from __future__ import annotations

import logging
import tomllib
from contextlib import contextmanager
from pathlib import Path

import pytest

from oa_configurator import (
    ConfigurationError,
    ConnectionConfig,
    StackConfig,
    StackConfigValidationError,
)
from oa_configurator.io import save_stack_config
from oa_configurator.loader import (
    DEFAULT_CONFIG_PATH,
    ENV_CONFIG_PATH,
    _ConfigCache,
    _normalize_path,
    _resolve_config_path,
    invalidate_cache,
    load_stack_config_from_path,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every test starts and ends with an empty process-local cache. Tests
    must never see another test's cached entries, or leak their own."""
    invalidate_cache()
    yield
    invalidate_cache()


@contextmanager
def _captured_warnings():
    """Capture warnings straight off the loader's own logger.

    Deliberately not ``caplog``: other test modules call ``configure_logging``
    and leave the ``oa_configurator`` logger with handlers and ``propagate``
    turned off, so whether a record reaches the root logger depends on test
    ordering. Attaching here and isolating the logger makes the assertion about
    this function rather than about global logging state.
    """
    logger = logging.getLogger("oa_configurator.loader")
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Capture(level=logging.WARNING)
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        yield messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def _make_config_file(tmp_path: Path, **connection_kwargs) -> Path:
    path = tmp_path / "config.toml"
    save_stack_config(
        StackConfig.for_session(
            connections={"cdm": ConnectionConfig(dialect="sqlite", **connection_kwargs)},
        ),
        path=path,
    )
    return path


class TestNormalizePath:
    def test_expands_home(self):
        result = _normalize_path("~/some/config.toml")
        assert result.is_absolute()
        assert "~" not in str(result)
        assert str(result).startswith(str(Path.home()))

    def test_resolves_relative_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _normalize_path("relative/config.toml")
        assert result == (tmp_path / "relative" / "config.toml").resolve()

    def test_absolute_path_stays_equivalent(self, tmp_path):
        target = tmp_path / "config.toml"
        assert _normalize_path(target) == target.resolve()


class TestDefaultConfigPath:
    def test_is_absolute_under_home(self):
        assert DEFAULT_CONFIG_PATH.is_absolute()
        assert DEFAULT_CONFIG_PATH == Path.home() / ".config" / "omop" / "config.toml"


class TestResolveConfigPath:
    def test_no_env_var_returns_default(self, monkeypatch):
        monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
        assert _resolve_config_path() == DEFAULT_CONFIG_PATH

    def test_env_var_points_to_existing_toml(self, tmp_path, monkeypatch):
        target = tmp_path / "custom.toml"
        target.write_text("")
        monkeypatch.setenv(ENV_CONFIG_PATH, str(target))
        assert _resolve_config_path() == target.resolve()

    def test_env_var_expands_and_resolves(self, tmp_path, monkeypatch):
        target = tmp_path / "custom.toml"
        target.write_text("")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(ENV_CONFIG_PATH, "custom.toml")
        assert _resolve_config_path() == target.resolve()

    def test_env_var_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_CONFIG_PATH, str(tmp_path / "does_not_exist.toml"))
        with pytest.raises(FileNotFoundError):
            _resolve_config_path()

    def test_env_var_non_toml_suffix_raises(self, tmp_path, monkeypatch):
        target = tmp_path / "custom.json"
        target.write_text("")
        monkeypatch.setenv(ENV_CONFIG_PATH, str(target))
        with pytest.raises(ValueError, match=r"\.toml"):
            _resolve_config_path()


class TestLoadFromPath:
    def test_loads_valid_config(self, tmp_path):
        path = _make_config_file(tmp_path)
        config = load_stack_config_from_path(path)
        assert config.connections["cdm"].dialect == "sqlite"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_stack_config_from_path(tmp_path / "does_not_exist.toml")

    def test_malformed_toml_raises_value_error(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("not [valid [ toml")
        with pytest.raises(ValueError, match="Malformed TOML"):
            load_stack_config_from_path(path)

    def test_binds_loaded_path(self, tmp_path):
        path = _make_config_file(tmp_path)
        config = load_stack_config_from_path(path)
        assert config.loaded_path == path.resolve()


class TestLoadFromPathCaching:
    """Exercises the real cache through load_stack_config_from_path, not just _ConfigCache
    in isolation. Proves the whole read path, not just the cache class."""

    def test_second_load_is_a_cache_hit(self, tmp_path, monkeypatch):
        path = _make_config_file(tmp_path)
        calls: list[str] = []
        real_loads = tomllib.loads

        def counting_loads(text, **kwargs):
            calls.append(text)
            return real_loads(text, **kwargs)

        monkeypatch.setattr("oa_configurator.loader.tomllib.loads", counting_loads)

        first = load_stack_config_from_path(path)
        second = load_stack_config_from_path(path)

        assert len(calls) == 1, "second load re-parsed the file instead of hitting the cache"
        assert first.connections["cdm"].dialect == second.connections["cdm"].dialect

    def test_mutating_one_load_does_not_affect_another(self, tmp_path):
        path = _make_config_file(tmp_path)
        first = load_stack_config_from_path(path)
        first.connections["cdm"].dialect = "mutated"

        second = load_stack_config_from_path(path)

        assert second.connections["cdm"].dialect == "sqlite"

    def test_content_change_invalidates_cache(self, tmp_path):
        path = _make_config_file(tmp_path)
        first = load_stack_config_from_path(path)
        assert first.connections["cdm"].dialect == "sqlite"

        save_stack_config(
            StackConfig.for_session(
                connections={"cdm": ConnectionConfig(dialect="postgresql+psycopg", host="db")},
            ),
            path=path,
        )
        second = load_stack_config_from_path(path)
        assert second.connections["cdm"].dialect == "postgresql+psycopg"

    def test_invalidate_cache_forces_reparse_even_without_content_change(self, tmp_path, monkeypatch):
        path = _make_config_file(tmp_path)
        calls: list[str] = []
        real_loads = tomllib.loads

        def counting_loads(text, **kwargs):
            calls.append(text)
            return real_loads(text, **kwargs)

        monkeypatch.setattr("oa_configurator.loader.tomllib.loads", counting_loads)

        load_stack_config_from_path(path)
        assert len(calls) == 1
        invalidate_cache()
        load_stack_config_from_path(path)
        assert len(calls) == 2


class TestConfigCache:
    """Unit-level coverage of _ConfigCache itself, independent of the TOML
    read/parse path. Isolates the deep-copy and key-matching contract."""

    def test_put_then_get_returns_a_deep_copy(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("")
        st = path.stat()
        original = StackConfig.for_session(connections={"cdm": ConnectionConfig(dialect="sqlite")})

        _ConfigCache.put(path, st, original)
        retrieved = _ConfigCache.get(path, st)

        assert retrieved is not None
        assert retrieved == original
        assert retrieved is not original
        assert retrieved.connections["cdm"] is not original.connections["cdm"]

    def test_get_miss_returns_none(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("")
        st = path.stat()
        assert _ConfigCache.get(path, st) is None

    def test_clear_removes_all_entries(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("")
        st = path.stat()
        _ConfigCache.put(path, st, StackConfig.for_session())
        _ConfigCache.clear()
        assert _ConfigCache.get(path, st) is None


class TestLoadStackConfigFromPathIsPublic:
    """Promoted from ``_load_from_path``.

    The private version was being reimplemented by consumers, and the copies
    dropped the loose-permissions warning -- a security behaviour nobody should
    have to re-derive to get.
    """

    def test_exported_from_the_package_root(self):
        import oa_configurator

        assert "load_stack_config_from_path" in oa_configurator.__all__
        assert (
            oa_configurator.load_stack_config_from_path is load_stack_config_from_path
        )

    def test_group_readable_file_warns_that_it_holds_passwords(self, tmp_path):
        path = _make_config_file(tmp_path)
        path.chmod(0o644)

        with _captured_warnings() as messages:
            load_stack_config_from_path(path)

        assert any("loose permissions" in message for message in messages)
        assert any("chmod 600" in message for message in messages)

    def test_owner_only_file_is_silent(self, tmp_path):
        path = _make_config_file(tmp_path)
        path.chmod(0o600)

        with _captured_warnings() as messages:
            load_stack_config_from_path(path)

        assert messages == []


class TestLoadStackConfigFromPathErrors:
    """A broken config file is the error most likely to be pasted into an issue.

    So the diagnosis must name the field that is wrong and never carry the value
    that was rejected.
    """

    def test_validation_failure_names_the_offending_field(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            '[connections.cdm]\n'
            'dialect = "sqlite"\n'
            'port = "not-a-port"\n'
            'password = "s3cret-CANARY"\n'
        )

        with pytest.raises(StackConfigValidationError) as excinfo:
            load_stack_config_from_path(path)

        message = str(excinfo.value)
        assert "connections.cdm.port" in message
        assert str(path) in message

    def test_validation_failure_never_echoes_the_rejected_value(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            '[connections.cdm]\n'
            'dialect = "sqlite"\n'
            'port = "s3cret-CANARY"\n'
        )

        with pytest.raises(StackConfigValidationError) as excinfo:
            load_stack_config_from_path(path)

        assert "s3cret-CANARY" not in str(excinfo.value)
        assert not any(
            "s3cret-CANARY" in str(error) for error in excinfo.value.errors()
        )

    def test_malformed_toml_names_the_file_without_echoing_its_contents(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('password = "s3cret-CANARY"\ngarbage here\n')

        with pytest.raises(ConfigurationError) as excinfo:
            load_stack_config_from_path(path)

        message = str(excinfo.value)
        assert str(path) in message
        assert "s3cret-CANARY" not in message

    def test_malformed_toml_is_still_a_value_error(self, tmp_path):
        """ConfigurationError subclasses ValueError, so existing handlers keep working."""
        path = tmp_path / "config.toml"
        path.write_text("not [valid [ toml")

        with pytest.raises(ValueError, match="Malformed TOML"):
            load_stack_config_from_path(path)
