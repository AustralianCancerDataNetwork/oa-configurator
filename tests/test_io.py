"""Tests for io.py: write_env_file, save_stack_config."""

from __future__ import annotations

import tomllib

from oa_configurator import Resolver, StackConfig, ConnectionConfig, CDMDatabaseConfig
from oa_configurator.io import save_stack_config, write_env_file


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
                "cdm": ConnectionConfig(dialect="postgresql+psycopg", host="cdm.host", port=5432, user="u", password="p", database_name="cdm"),
                "emb": ConnectionConfig(dialect="postgresql+psycopg", host="emb.host", port=5433, user="eu", password="ep", database_name="embeddings"),
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
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"default": CDMDatabaseConfig(connection="db", schema_name="omop")},
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


class TestSaveStackConfig:
    def test_creates_file(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"default": CDMDatabaseConfig(connection="db", schema_name="omop")},
        )
        out = tmp_path / "config.toml"
        save_stack_config(cfg, out)
        assert out.exists()

    def test_round_trip(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={
                "cdm": ConnectionConfig(dialect="postgresql+psycopg", host="localhost", port=5432, user="omop", password="pass", database_name="omop_cdm")
            },
            databases={"default": CDMDatabaseConfig(connection="cdm", schema_name="omop")},
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
            databases={"default": CDMDatabaseConfig(connection="db", schema_name="omop")},
        )
        out = tmp_path / "config.toml"
        save_stack_config(cfg, out)
        data = tomllib.loads(out.read_text())
        assert "password" not in data["connections"]["db"]

    def test_creates_parent_dirs(self, tmp_path):
        out = tmp_path / "nested" / "dirs" / "config.toml"
        save_stack_config(StackConfig.for_session(), out)
        assert out.exists()
