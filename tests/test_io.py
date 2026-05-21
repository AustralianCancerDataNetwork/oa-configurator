"""Tests for io.py — write_env_file."""

from __future__ import annotations

from pathlib import Path

import pytest

from oa_configurator import Resolver, StackConfig
from oa_configurator.io import write_env_file


def _make_cdm_stack() -> StackConfig:
    return StackConfig.for_session(
        connections={
            "cdm": {
                "dialect": "postgresql+psycopg2",
                "host": "db.example.com",
                "port": 5432,
                "user": "omop_user",
                "password": "s3cr3t",
                "database": "omop_cdm",
            }
        },
        resources={
            "default": {
                "primary_db": "cdm",
                "cdm_schema": "omop",
            }
        },
    )


class TestWriteEnvFile:
    def test_creates_file(self, tmp_path):
        cfg = _make_cdm_stack()
        out = tmp_path / "config.env"
        write_env_file(Resolver(cfg), path=out)
        assert out.exists()

    def test_cdm_host_port_user(self, tmp_path):
        cfg = _make_cdm_stack()
        out = tmp_path / "config.env"
        write_env_file(Resolver(cfg), path=out)
        content = out.read_text()
        assert "OMOP_CDM_DB_HOST=db.example.com" in content
        assert "OMOP_CDM_DB_PORT=5432" in content
        assert "OMOP_CDM_DB_USER=omop_user" in content

    def test_cdm_password_written(self, tmp_path):
        cfg = _make_cdm_stack()
        out = tmp_path / "config.env"
        write_env_file(Resolver(cfg), path=out)
        content = out.read_text()
        assert "OMOP_CDM_DB_PASSWORD=s3cr3t" in content

    def test_cdm_database_and_driver(self, tmp_path):
        cfg = _make_cdm_stack()
        out = tmp_path / "config.env"
        write_env_file(Resolver(cfg), path=out)
        content = out.read_text()
        assert "OMOP_CDM_DB_NAME=omop_cdm" in content
        assert "OMOP_CDM_DB_DRIVER=postgresql+psycopg2" in content

    def test_engine_url_written(self, tmp_path):
        cfg = _make_cdm_stack()
        out = tmp_path / "config.env"
        write_env_file(Resolver(cfg), path=out)
        content = out.read_text()
        assert "ENGINE=" in content
        assert "postgresql" in content

    def test_no_omop_emb_lines_when_resource_absent(self, tmp_path):
        cfg = _make_cdm_stack()
        out = tmp_path / "config.env"
        write_env_file(Resolver(cfg), path=out)
        content = out.read_text()
        assert "OMOP_EMB_DB_" not in content

    def test_omop_emb_lines_when_resource_present(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={
                "cdm": {
                    "dialect": "postgresql+psycopg2",
                    "host": "cdm.host",
                    "port": 5432,
                    "user": "u",
                    "password": "p",
                    "database": "cdm",
                },
                "emb": {
                    "dialect": "postgresql+psycopg2",
                    "host": "emb.host",
                    "port": 5433,
                    "user": "eu",
                    "password": "ep",
                    "database": "embeddings",
                },
            },
            resources={
                "default": {"primary_db": "cdm", "cdm_schema": "omop"},
                "omop_emb": {"primary_db": "emb", "cdm_schema": "emb"},
            },
            tools={
                "omop_emb": {"extra": {"backend": "postgres"}},
            },
        )
        out = tmp_path / "config.env"
        write_env_file(Resolver(cfg), path=out)
        content = out.read_text()
        assert "OMOP_EMB_DB_HOST=emb.host" in content
        assert "OMOP_EMB_BACKEND=postgres" in content

    def test_returns_path(self, tmp_path):
        cfg = _make_cdm_stack()
        out = tmp_path / "config.env"
        result = write_env_file(Resolver(cfg), path=out)
        assert result == out
