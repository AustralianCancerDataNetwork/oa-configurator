"""Tests for revision tokens and cooperative locking on config writes."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from filelock import FileLock

import oa_configurator.io as io_module
from oa_configurator import (
    ConfigSaveError,
    ConnectionConfig,
    StackConfig,
    StaleConfigRevisionError,
    load_stack_config,
    load_stack_config_with_revision,
    save_stack_config,
    stack_config_revision,
)
from oa_configurator.domains.resources.sql import Dialect

_PASSWORD = "revision-canary-pw"


def _stack(name: str = "first", password: str | None = None) -> StackConfig:
    return StackConfig.for_session(
        connections={
            name: ConnectionConfig(
                dialect=Dialect.SQLITE, database_name=":memory:", password=password
            )
        }
    )


def _leftovers(directory: Path, keep: set[str]) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name not in keep)


class TestRevisionToken:
    def test_missing_file_has_a_stable_token(self, tmp_path):
        path = tmp_path / "config.toml"

        assert stack_config_revision(path) == stack_config_revision(path)

    def test_token_changes_with_the_bytes_and_not_with_a_rewrite_of_the_same_bytes(self, tmp_path):
        path = tmp_path / "config.toml"
        absent = stack_config_revision(path)
        save_stack_config(_stack("first"), path)
        first = stack_config_revision(path)
        save_stack_config(_stack("first"), path)
        same_again = stack_config_revision(path)
        save_stack_config(_stack("second"), path)

        assert len({absent, first, stack_config_revision(path)}) == 3
        assert same_again == first

    def test_token_does_not_expose_file_content(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack(password=_PASSWORD), path)

        assert _PASSWORD not in stack_config_revision(path)

    def test_token_is_available_for_an_unparsable_file(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("not = [valid")

        assert stack_config_revision(path) != stack_config_revision(tmp_path / "missing.toml")

    def test_load_returns_the_token_of_the_bytes_it_parsed(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)

        config, revision = load_stack_config_with_revision(path)

        assert list(config.connections) == ["first"]
        assert revision == stack_config_revision(path)

    def test_cached_load_returns_the_same_token(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)

        first = load_stack_config_with_revision(path)[1]
        second = load_stack_config_with_revision(path)[1]

        assert first == second

    def test_token_stays_with_the_bytes_read_when_the_file_changes_afterwards(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)
        config, revision = load_stack_config_with_revision(path)

        save_stack_config(_stack("someone_else"), path)

        assert list(config.connections) == ["first"]
        assert revision != stack_config_revision(path)

    def test_load_without_revision_is_unchanged(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)

        assert load_stack_config(path) == load_stack_config_with_revision(path)[0]


class TestConditionalSave:
    def test_matching_token_saves_and_returns_the_path(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)
        _, revision = load_stack_config_with_revision(path)

        written = save_stack_config(_stack("second"), path, expected_revision=revision)

        assert written == path
        assert list(load_stack_config(path).connections) == ["second"]

    def test_new_token_after_save_allows_the_next_conditional_save(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)
        save_stack_config(_stack("second"), path, expected_revision=stack_config_revision(path))

        save_stack_config(_stack("third"), path, expected_revision=stack_config_revision(path))

        assert list(load_stack_config(path).connections) == ["third"]

    def test_stale_token_is_refused_and_nothing_is_written(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)
        _, stale = load_stack_config_with_revision(path)
        save_stack_config(_stack("someone_else"), path)
        before = path.read_bytes()
        backup_before = path.with_name("config.toml.bak").read_bytes()

        with pytest.raises(StaleConfigRevisionError):
            save_stack_config(_stack("mine"), path, expected_revision=stale)

        assert path.read_bytes() == before
        assert path.with_name("config.toml.bak").read_bytes() == backup_before
        assert _leftovers(tmp_path, {"config.toml", "config.toml.bak", "config.toml.lock"}) == []

    def test_stale_error_is_a_config_save_error_without_content(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack(password=_PASSWORD), path)
        stale = stack_config_revision(path)
        save_stack_config(_stack("other", password=_PASSWORD), path)

        with pytest.raises(StaleConfigRevisionError) as exc_info:
            save_stack_config(_stack("mine", password=_PASSWORD), path, expected_revision=stale)

        assert isinstance(exc_info.value, ConfigSaveError)
        assert isinstance(exc_info.value, OSError)
        assert _PASSWORD not in str(exc_info.value)

    def test_absent_token_allows_the_first_save(self, tmp_path):
        path = tmp_path / "config.toml"

        save_stack_config(_stack("first"), path, expected_revision=stack_config_revision(path))

        assert path.exists()

    def test_absent_token_is_stale_once_the_file_appears(self, tmp_path):
        path = tmp_path / "config.toml"
        absent = stack_config_revision(path)
        save_stack_config(_stack("someone_else"), path)

        with pytest.raises(StaleConfigRevisionError):
            save_stack_config(_stack("mine"), path, expected_revision=absent)

        assert list(load_stack_config(path).connections) == ["someone_else"]

    def test_no_token_still_replaces_unconditionally(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)
        save_stack_config(_stack("someone_else"), path)

        save_stack_config(_stack("mine"), path)

        assert list(load_stack_config(path).connections) == ["mine"]


class TestLock:
    def test_lock_file_is_private_empty_and_kept(self, tmp_path):
        path = tmp_path / "config.toml"

        save_stack_config(_stack(), path)

        lock = tmp_path / "config.toml.lock"
        assert lock.exists()
        assert lock.read_bytes() == b""
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600

    def test_save_gives_up_when_another_writer_holds_the_lock(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)
        before = path.read_bytes()

        with (
            FileLock(tmp_path / "config.toml.lock"),
            pytest.raises(ConfigSaveError, match="another process is saving"),
        ):
            save_stack_config(_stack("mine"), path, lock_timeout=0.2)

        assert path.read_bytes() == before
        assert _leftovers(tmp_path, {"config.toml", "config.toml.bak", "config.toml.lock"}) == []

    def test_lock_is_released_after_a_stale_refusal(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)
        with pytest.raises(StaleConfigRevisionError):
            save_stack_config(_stack("mine"), path, expected_revision="sha256:wrong")

        save_stack_config(_stack("second"), path, lock_timeout=0.2)

        assert list(load_stack_config(path).connections) == ["second"]

    def test_lock_is_released_after_a_rolled_back_save(self, tmp_path, monkeypatch):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)

        def fail(*args, **kwargs):
            raise ConfigSaveError("verification failed")

        monkeypatch.setattr(io_module, "_verify_saved_config", fail)
        with pytest.raises(ConfigSaveError, match="previous state was restored"):
            save_stack_config(_stack("mine"), path)
        monkeypatch.undo()

        save_stack_config(_stack("second"), path, lock_timeout=0.2)

        assert list(load_stack_config(path).connections) == ["second"]

    def test_only_one_of_many_threads_wins_with_the_same_token(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)
        revision = stack_config_revision(path)
        barrier = threading.Barrier(8)
        outcomes: list[str] = []

        def attempt(index: int) -> None:
            config = _stack(f"writer_{index}")
            barrier.wait()
            try:
                save_stack_config(config, path, expected_revision=revision)
                outcomes.append("saved")
            except StaleConfigRevisionError:
                outcomes.append("stale")

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(outcomes) == ["saved"] + ["stale"] * 7
        assert next(iter(load_stack_config(path).connections)).startswith("writer_")

    def test_check_and_replace_are_not_interleaved_by_a_second_writer(self, tmp_path, monkeypatch):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)
        revision = stack_config_revision(path)
        original_copy = io_module._copy_secure_temp
        inside_lock = threading.Event()
        calls: list[int] = []

        def slow_copy(source, target):
            if not calls:
                calls.append(1)
                inside_lock.set()
                time.sleep(0.4)
            return original_copy(source, target)

        monkeypatch.setattr(io_module, "_copy_secure_temp", slow_copy)
        outcomes: list[str] = []

        def attempt(name: str) -> None:
            try:
                save_stack_config(_stack(name), path, expected_revision=revision)
                outcomes.append("saved")
            except StaleConfigRevisionError:
                outcomes.append("stale")

        first = threading.Thread(target=attempt, args=("writer_a",))
        second = threading.Thread(target=attempt, args=("writer_b",))
        first.start()
        assert inside_lock.wait(timeout=5)
        second.start()
        first.join()
        second.join()

        assert sorted(outcomes) == ["saved", "stale"]

    def test_unconditional_threads_leave_a_valid_file(self, tmp_path):
        path = tmp_path / "config.toml"
        barrier = threading.Barrier(6)
        errors: list[OSError] = []

        def attempt(index: int) -> None:
            barrier.wait()
            try:
                save_stack_config(_stack(f"writer_{index}"), path)
            except OSError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert next(iter(load_stack_config(path).connections)).startswith("writer_")

    def test_only_one_of_many_processes_wins_with_the_same_token(self, tmp_path):
        path = tmp_path / "config.toml"
        save_stack_config(_stack("first"), path)
        revision = stack_config_revision(path)
        go = tmp_path / "go"
        script = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from oa_configurator import (ConnectionConfig, StackConfig, StaleConfigRevisionError,"
            " save_stack_config)\n"
            "from oa_configurator.domains.resources.sql import Dialect\n"
            "name, path, revision, go = sys.argv[1:]\n"
            "cfg = StackConfig.for_session(connections={name: ConnectionConfig("
            "dialect=Dialect.SQLITE, database_name=':memory:')})\n"
            "while not Path(go).exists():\n"
            "    time.sleep(0.01)\n"
            "try:\n"
            "    save_stack_config(cfg, Path(path), expected_revision=revision)\n"
            "    print('saved')\n"
            "except StaleConfigRevisionError:\n"
            "    print('stale')\n"
        )
        env = {**os.environ, "OA_CONFIG_PATH": str(path)}
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, f"writer_{i}", str(path), revision, str(go)],
                stdout=subprocess.PIPE,
                text=True,
                env=env,
            )
            for i in range(4)
        ]
        go.write_text("")
        outcomes = [process.communicate(timeout=60)[0].strip() for process in processes]

        assert sorted(outcomes) == ["saved"] + ["stale"] * 3
        assert next(iter(load_stack_config(path).connections)).startswith("writer_")
