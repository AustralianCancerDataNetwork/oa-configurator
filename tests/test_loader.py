"""Tests for loader.py: path normalization, OA_CONFIG_PATH resolution, and
the process-local StackConfig cache keyed on file identity (mtime + size)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from oa_configurator.io import save_stack_config
from oa_configurator.loader import (
    DEFAULT_CONFIG_PATH,
    ENV_CONFIG_PATH,
    _ConfigCache,
    _load_from_path,
    _normalize_path,
    _resolve_config_path,
    invalidate_cache,
)
from oa_configurator.stack_config import ConnectionConfig, StackConfig


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every test starts and ends with an empty process-local cache. Tests
    must never see another test's cached entries, or leak their own."""
    invalidate_cache()
    yield
    invalidate_cache()


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
        config = _load_from_path(path)
        assert config.connections["cdm"].dialect == "sqlite"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _load_from_path(tmp_path / "does_not_exist.toml")

    def test_malformed_toml_raises_value_error(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("not [valid [ toml")
        with pytest.raises(ValueError, match="Malformed TOML"):
            _load_from_path(path)

    def test_binds_loaded_path(self, tmp_path):
        path = _make_config_file(tmp_path)
        config = _load_from_path(path)
        assert config.loaded_path == path.resolve()


class TestLoadFromPathCaching:
    """Exercises the real cache through _load_from_path, not just _ConfigCache
    in isolation. Proves the whole read path, not just the cache class."""

    def test_second_load_is_a_cache_hit(self, tmp_path, monkeypatch):
        path = _make_config_file(tmp_path)
        calls: list[str] = []
        real_loads = tomllib.loads

        def counting_loads(text, **kwargs):
            calls.append(text)
            return real_loads(text, **kwargs)

        monkeypatch.setattr("oa_configurator.loader.tomllib.loads", counting_loads)

        first = _load_from_path(path)
        second = _load_from_path(path)

        assert len(calls) == 1, "second load re-parsed the file instead of hitting the cache"
        assert first.connections["cdm"].dialect == second.connections["cdm"].dialect

    def test_mutating_one_load_does_not_affect_another(self, tmp_path):
        path = _make_config_file(tmp_path)
        first = _load_from_path(path)
        first.connections["cdm"].dialect = "mutated"

        second = _load_from_path(path)

        assert second.connections["cdm"].dialect == "sqlite"

    def test_content_change_invalidates_cache(self, tmp_path):
        path = _make_config_file(tmp_path)
        first = _load_from_path(path)
        assert first.connections["cdm"].dialect == "sqlite"

        save_stack_config(
            StackConfig.for_session(
                connections={"cdm": ConnectionConfig(dialect="postgresql+psycopg", host="db")},
            ),
            path=path,
        )
        second = _load_from_path(path)
        assert second.connections["cdm"].dialect == "postgresql+psycopg"

    def test_invalidate_cache_forces_reparse_even_without_content_change(self, tmp_path, monkeypatch):
        path = _make_config_file(tmp_path)
        calls: list[str] = []
        real_loads = tomllib.loads

        def counting_loads(text, **kwargs):
            calls.append(text)
            return real_loads(text, **kwargs)

        monkeypatch.setattr("oa_configurator.loader.tomllib.loads", counting_loads)

        _load_from_path(path)
        assert len(calls) == 1
        invalidate_cache()
        _load_from_path(path)
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
