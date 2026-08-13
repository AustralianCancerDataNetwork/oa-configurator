"""Tests for io.py: write_env_file, save_stack_config."""

from __future__ import annotations

import errno
import os
import stat
import tomllib
from pathlib import Path

import pytest

import oa_configurator.io as io_module
from oa_configurator import ConfigSaveError as PublicConfigSaveError
from oa_configurator import (
    CDMDatabaseConfig,
    ConnectionConfig,
    GenericDatabaseConfig,
    LoggingConfig,
    ModelConfig,
    ProviderConfig,
    Resolver,
    StackConfig,
    VectorStoreConfig,
)
from oa_configurator.io import ConfigSaveError, save_stack_config, write_env_file


def _make_cdm_stack() -> StackConfig:
    return StackConfig.for_session(
        connections={
            "cdm": ConnectionConfig(
                dialect="postgresql+psycopg",
                host="db.example.com",
                port=5432,
                user="omop_user",
                password="s3cr3t",
                database_name="omop_cdm",
            )
        },
        databases={
            "default": CDMDatabaseConfig(connection="cdm", schema_name="omop"),
        },
    )


class TestWriteEnvFile:
    def test_creates_file(self, tmp_path):
        out = tmp_path / "config.env"
        write_env_file(Resolver(_make_cdm_stack()), path=out)
        assert out.exists()

    def test_default_database_host_port_user(self, tmp_path):
        out = tmp_path / "config.env"
        write_env_file(Resolver(_make_cdm_stack()), path=out)
        content = out.read_text()
        assert "DEFAULT_DB_HOST=db.example.com" in content
        assert "DEFAULT_DB_PORT=5432" in content
        assert "DEFAULT_DB_USER=omop_user" in content

    def test_default_database_password(self, tmp_path):
        out = tmp_path / "config.env"
        write_env_file(Resolver(_make_cdm_stack()), path=out)
        assert "DEFAULT_DB_PASSWORD=s3cr3t" in out.read_text()

    def test_default_database_name_and_driver(self, tmp_path):
        out = tmp_path / "config.env"
        write_env_file(Resolver(_make_cdm_stack()), path=out)
        content = out.read_text()
        assert "DEFAULT_DB_DATABASE_NAME=omop_cdm" in content
        assert "DEFAULT_DB_DIALECT=postgresql+psycopg" in content

    def test_default_database_url_written(self, tmp_path):
        out = tmp_path / "config.env"
        write_env_file(Resolver(_make_cdm_stack()), path=out)
        content = out.read_text()
        assert "DEFAULT_DB_URL=" in content
        assert "postgresql" in content

    def test_no_omop_emb_lines_when_database_absent(self, tmp_path):
        out = tmp_path / "config.env"
        write_env_file(Resolver(_make_cdm_stack()), path=out)
        assert "OMOP_EMB_DB_" not in out.read_text()

    def test_generic_database_prefix(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={
                "cdm": ConnectionConfig(
                    dialect="postgresql+psycopg",
                    host="cdm.host",
                    port=5432,
                    user="u",
                    password="p",
                    database_name="cdm",
                ),
                "emb": ConnectionConfig(
                    dialect="postgresql+psycopg",
                    host="emb.host",
                    port=5433,
                    user="eu",
                    password="ep",
                    database_name="embeddings",
                ),
            },
            databases={
                "default": CDMDatabaseConfig(connection="cdm", schema_name="omop"),
                "omop_emb": CDMDatabaseConfig(connection="emb", schema_name="emb"),
            },
            tools={
                "omop_emb": {"backend": "pgvector"},
            },
        )
        out = tmp_path / "config.env"
        write_env_file(Resolver(cfg), path=out)
        content = out.read_text()
        assert "OMOP_EMB_DB_HOST=emb.host" in content
        assert "OMOP_EMB_BACKEND=pgvector" in content

    def test_tool_extra_scalars_exported(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={
                "db": ConnectionConfig(dialect="sqlite", database_name=":memory:")
            },
            databases={
                "default": CDMDatabaseConfig(connection="db", schema_name="omop")
            },
            tools={"my_pkg": {"foo": "bar", "count": 3}},
        )
        out = tmp_path / "config.env"
        write_env_file(Resolver(cfg), path=out)
        content = out.read_text()
        assert "MY_PKG_FOO=bar" in content
        assert "MY_PKG_COUNT=3" in content

    def test_returns_path(self, tmp_path):
        out = tmp_path / "config.env"
        assert write_env_file(Resolver(_make_cdm_stack()), path=out) == out

    def test_written_with_owner_only_permissions(self, tmp_path):
        out = tmp_path / "config.env"
        write_env_file(Resolver(_make_cdm_stack()), path=out)
        assert stat.S_IMODE(out.stat().st_mode) == 0o600


class TestSaveStackConfig:
    def test_save_error_is_public(self):
        assert PublicConfigSaveError is ConfigSaveError

    def test_creates_file(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={
                "db": ConnectionConfig(dialect="sqlite", database_name=":memory:")
            },
            databases={
                "default": CDMDatabaseConfig(connection="db", schema_name="omop")
            },
        )
        out = tmp_path / "config.toml"
        save_stack_config(cfg, out)
        assert out.exists()

    def test_round_trip(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={
                "cdm": ConnectionConfig(
                    dialect="postgresql+psycopg",
                    host="localhost",
                    port=5432,
                    user="omop",
                    password="pass",
                    database_name="omop_cdm",
                )
            },
            databases={
                "default": CDMDatabaseConfig(connection="cdm", schema_name="omop")
            },
        )
        out = tmp_path / "config.toml"
        save_stack_config(cfg, out)
        data = tomllib.loads(out.read_text())
        assert data["connections"]["cdm"]["host"] == "localhost"
        assert data["databases"]["default"]["schema_name"] == "omop"

    def test_default_logging_not_written(self, tmp_path):
        out = tmp_path / "config.toml"
        save_stack_config(StackConfig.for_session(), out)
        assert "logging" not in tomllib.loads(out.read_text())

    def test_none_values_stripped(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite")},
            databases={
                "default": CDMDatabaseConfig(connection="db", schema_name="omop")
            },
        )
        out = tmp_path / "config.toml"
        save_stack_config(cfg, out)
        data = tomllib.loads(out.read_text())
        assert "password" not in data["connections"]["db"]

    def test_creates_parent_dirs(self, tmp_path):
        out = tmp_path / "nested" / "dirs" / "config.toml"
        save_stack_config(StackConfig.for_session(), out)
        assert out.exists()

    def test_written_with_owner_only_permissions(self, tmp_path):
        out = tmp_path / "config.toml"
        save_stack_config(StackConfig.for_session(), out)
        assert stat.S_IMODE(out.stat().st_mode) == 0o600

    def test_existing_file_is_refreshed_as_secure_backup(self, tmp_path):
        out = tmp_path / "config.toml"
        save_stack_config(
            StackConfig.for_session(tools={"demo": {"value": "old"}}), out
        )
        old_bytes = out.read_bytes()

        save_stack_config(
            StackConfig.for_session(tools={"demo": {"value": "new"}}), out
        )

        backup = tmp_path / "config.toml.bak"
        assert backup.read_bytes() == old_bytes
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    def test_serialization_failure_does_not_touch_destination(
        self, tmp_path, monkeypatch
    ):
        out = tmp_path / "config.toml"
        out.write_text("original", encoding="utf-8")
        monkeypatch.setattr(
            io_module.tomli_w,
            "dumps",
            lambda payload: (_ for _ in ()).throw(TypeError("bad TOML")),
        )

        with pytest.raises(TypeError, match="bad TOML"):
            save_stack_config(StackConfig.for_session(), out)

        assert out.read_text(encoding="utf-8") == "original"
        assert not (tmp_path / "config.toml.bak").exists()

    def test_revalidation_failure_does_not_touch_destination(self, tmp_path):
        out = tmp_path / "config.toml"
        out.write_text("original", encoding="utf-8")
        cfg = _make_cdm_stack()
        cfg.databases["default"].connection = "missing"

        with pytest.raises(ValueError, match="unknown connection"):
            save_stack_config(cfg, out)

        assert out.read_text(encoding="utf-8") == "original"
        assert not (tmp_path / "config.toml.bak").exists()

    def test_parent_creation_failure_happens_before_any_temp_write(self, tmp_path):
        parent = tmp_path / "not-a-directory"
        parent.write_text("occupied", encoding="utf-8")
        out = parent / "config.toml"

        with pytest.raises((FileExistsError, NotADirectoryError)):
            save_stack_config(StackConfig.for_session(), out)

        assert parent.read_text(encoding="utf-8") == "occupied"
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_candidate_is_mode_0600_before_secret_write(self, tmp_path, monkeypatch):
        observed_modes: list[int] = []
        real_write = io_module._write_and_sync

        def inspect_mode(stream, data):
            observed_modes.append(stat.S_IMODE(os.fstat(stream.fileno()).st_mode))
            real_write(stream, data)

        monkeypatch.setattr(io_module, "_write_and_sync", inspect_mode)
        save_stack_config(_make_cdm_stack(), tmp_path / "config.toml")

        assert observed_modes == [0o600]

    def test_failed_candidate_write_preserves_destination_and_cleans_temp(
        self, tmp_path, monkeypatch
    ):
        out = tmp_path / "config.toml"
        out.write_text("original", encoding="utf-8")

        def fail_write(stream, data):
            stream.write(data[:5])
            raise OSError(errno.ENOSPC, "disk full")

        monkeypatch.setattr(io_module, "_write_and_sync", fail_write)
        with pytest.raises(OSError, match="disk full"):
            save_stack_config(_make_cdm_stack(), out)

        assert out.read_text(encoding="utf-8") == "original"
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_candidate_fsync_failure_preserves_destination(self, tmp_path, monkeypatch):
        out = tmp_path / "config.toml"
        out.write_text("original", encoding="utf-8")
        real_fsync = os.fsync
        calls = 0

        def fail_first_fsync(fd):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(errno.EIO, "candidate fsync failed")
            real_fsync(fd)

        monkeypatch.setattr(io_module.os, "fsync", fail_first_fsync)
        with pytest.raises(OSError, match="candidate fsync failed"):
            save_stack_config(StackConfig.for_session(), out)

        assert out.read_text(encoding="utf-8") == "original"
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_backup_creation_failure_preserves_destination(self, tmp_path, monkeypatch):
        out = tmp_path / "config.toml"
        out.write_text("original", encoding="utf-8")
        monkeypatch.setattr(
            io_module,
            "_copy_secure_temp",
            lambda source, target: (_ for _ in ()).throw(OSError("backup failed")),
        )

        with pytest.raises(OSError, match="backup failed"):
            save_stack_config(StackConfig.for_session(), out)

        assert out.read_text(encoding="utf-8") == "original"
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_backup_replace_failure_preserves_destination(self, tmp_path, monkeypatch):
        out = tmp_path / "config.toml"
        out.write_text("original", encoding="utf-8")
        real_replace = os.replace

        def fail_backup_replace(source, destination):
            if Path(destination).name == "config.toml.bak":
                raise OSError("backup replace failed")
            real_replace(source, destination)

        monkeypatch.setattr(io_module.os, "replace", fail_backup_replace)
        with pytest.raises(OSError, match="backup replace failed"):
            save_stack_config(StackConfig.for_session(), out)

        assert out.read_text(encoding="utf-8") == "original"
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_destination_replace_failure_leaves_original_and_backup(
        self, tmp_path, monkeypatch
    ):
        out = tmp_path / "config.toml"
        out.write_text("original", encoding="utf-8")
        real_replace = os.replace

        def fail_destination_replace(source, destination):
            if Path(destination) == out:
                raise OSError("destination replace failed")
            real_replace(source, destination)

        monkeypatch.setattr(io_module.os, "replace", fail_destination_replace)
        with pytest.raises(OSError, match="destination replace failed"):
            save_stack_config(StackConfig.for_session(), out)

        assert out.read_text(encoding="utf-8") == "original"
        assert (tmp_path / "config.toml.bak").read_text(encoding="utf-8") == "original"
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_directory_sync_failure_before_destination_swap_preserves_original(
        self, tmp_path, monkeypatch
    ):
        out = tmp_path / "config.toml"
        out.write_text("original", encoding="utf-8")
        monkeypatch.setattr(
            io_module,
            "_fsync_directory",
            lambda directory: (_ for _ in ()).throw(OSError("directory sync failed")),
        )

        with pytest.raises(OSError, match="directory sync failed"):
            save_stack_config(StackConfig.for_session(), out)

        assert out.read_text(encoding="utf-8") == "original"

    def test_post_swap_sync_failure_rolls_back_existing_destination(
        self, tmp_path, monkeypatch
    ):
        out = tmp_path / "config.toml"
        save_stack_config(
            StackConfig.for_session(tools={"demo": {"value": "old"}}), out
        )
        old_bytes = out.read_bytes()
        calls = 0

        def fail_destination_sync(directory):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("destination sync failed")

        monkeypatch.setattr(io_module, "_fsync_directory", fail_destination_sync)
        with pytest.raises(ConfigSaveError, match="previous state was restored"):
            save_stack_config(
                StackConfig.for_session(tools={"demo": {"value": "new"}}), out
            )

        assert out.read_bytes() == old_bytes
        assert (tmp_path / "config.toml.bak").read_bytes() == old_bytes

    def test_post_swap_sync_failure_removes_first_destination(
        self, tmp_path, monkeypatch
    ):
        out = tmp_path / "config.toml"
        calls = 0

        def fail_first_sync(directory):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("destination sync failed")

        monkeypatch.setattr(io_module, "_fsync_directory", fail_first_sync)
        with pytest.raises(ConfigSaveError, match="previous state was restored"):
            save_stack_config(StackConfig.for_session(), out)

        assert not out.exists()

    def test_cache_invalidation_happens_only_after_destination_swap(
        self, tmp_path, monkeypatch
    ):
        out = tmp_path / "config.toml"
        events: list[str] = []
        real_replace = os.replace

        def record_replace(source, destination):
            real_replace(source, destination)
            if Path(destination) == out:
                events.append("replace")

        monkeypatch.setattr(io_module.os, "replace", record_replace)
        monkeypatch.setattr(
            io_module, "invalidate_cache", lambda: events.append("invalidate")
        )
        monkeypatch.setattr(
            io_module,
            "_verify_saved_config",
            lambda path, expected: events.append("verify"),
        )

        save_stack_config(StackConfig.for_session(), out)

        assert events == ["replace", "invalidate", "verify", "invalidate"]

    def test_reload_verification_failure_restores_existing_destination(
        self, tmp_path, monkeypatch
    ):
        out = tmp_path / "config.toml"
        save_stack_config(
            StackConfig.for_session(tools={"demo": {"value": "old"}}), out
        )
        old_bytes = out.read_bytes()
        monkeypatch.setattr(
            io_module,
            "_verify_saved_config",
            lambda path, expected: (_ for _ in ()).throw(
                ValueError("verification failed")
            ),
        )

        with pytest.raises(ConfigSaveError, match="previous state was restored"):
            save_stack_config(
                StackConfig.for_session(tools={"demo": {"value": "new"}}), out
            )

        assert out.read_bytes() == old_bytes

    def test_reload_verification_failure_removes_new_destination(
        self, tmp_path, monkeypatch
    ):
        out = tmp_path / "config.toml"
        monkeypatch.setattr(
            io_module,
            "_verify_saved_config",
            lambda path, expected: (_ for _ in ()).throw(
                ValueError("verification failed")
            ),
        )

        with pytest.raises(ConfigSaveError, match="previous state was restored"):
            save_stack_config(StackConfig.for_session(), out)

        assert not out.exists()

    def test_rollback_failure_reports_recovery_failure(self, tmp_path, monkeypatch):
        out = tmp_path / "config.toml"
        save_stack_config(StackConfig.for_session(), out)
        monkeypatch.setattr(
            io_module,
            "_verify_saved_config",
            lambda path, expected: (_ for _ in ()).throw(
                ValueError("verification failed")
            ),
        )
        monkeypatch.setattr(
            io_module,
            "_restore_previous",
            lambda path, backup_path, previous_exists: (_ for _ in ()).throw(
                OSError("rollback failed")
            ),
        )

        with pytest.raises(
            ConfigSaveError, match="recovery also failed: rollback failed"
        ):
            save_stack_config(
                StackConfig.for_session(tools={"demo": {"value": "new"}}), out
            )

        assert list(tmp_path.glob(".*.tmp")) == []

    def test_round_trip_covers_every_top_level_section_and_secrets(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={
                "primary": ConnectionConfig(
                    dialect="postgresql+psycopg",
                    host="db.example.com",
                    user="omop",
                    password="connection-secret",
                    database_name="omop",
                )
            },
            databases={
                "cdm": CDMDatabaseConfig(connection="primary", schema_name="omop"),
                "generic": GenericDatabaseConfig(connection="primary"),
            },
            providers={
                "provider": ProviderConfig(
                    provider="openai", api_key="provider-secret"
                ),
            },
            models={
                "embedding": ModelConfig(provider="provider", model="embedding-model"),
            },
            vector_stores={
                "vectors": VectorStoreConfig(
                    backend_type="pgvector", database="generic"
                ),
            },
            tools={"demo": {"enabled": True, "nested": {"count": 2}}},
        )
        cfg.logging = LoggingConfig(level="INFO")
        out = tmp_path / "config.toml"

        save_stack_config(cfg, out)

        reloaded = io_module._verify_saved_config(out, cfg)
        assert reloaded is None
        content = out.read_text(encoding="utf-8")
        assert "connection-secret" in content
        assert "provider-secret" in content
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
