"""Tests for persistence.py — save_stack_config, patch_active_profile."""

from __future__ import annotations

import tomllib

import pytest

from oa_configurator import StackConfig
from oa_configurator.persistence import patch_active_profile, save_stack_config


class TestSaveStackConfig:
    def test_creates_file(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={"db": {"dialect": "sqlite", "database": ":memory:"}},
            resources={"default": {"primary_db": "db", "cdm_schema": "omop"}},
        )
        out = tmp_path / "config.toml"
        save_stack_config(cfg, out)
        assert out.exists()

    def test_round_trip(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={
                "cdm": {
                    "dialect": "postgresql+psycopg2",
                    "host": "localhost",
                    "port": 5432,
                    "user": "omop",
                    "password": "pass",
                    "database": "omop_cdm",
                }
            },
            resources={"default": {"primary_db": "cdm", "cdm_schema": "omop"}},
        )
        out = tmp_path / "config.toml"
        save_stack_config(cfg, out)

        data = tomllib.loads(out.read_text())
        assert data["connections"]["cdm"]["host"] == "localhost"
        assert data["connections"]["cdm"]["dialect"] == "postgresql+psycopg2"
        assert data["resources"]["default"]["cdm_schema"] == "omop"

    def test_default_logging_not_written(self, tmp_path):
        cfg = StackConfig.for_session()
        out = tmp_path / "config.toml"
        save_stack_config(cfg, out)
        data = tomllib.loads(out.read_text())
        assert "logging" not in data

    def test_none_values_stripped(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={"db": {"dialect": "sqlite"}},
            resources={"default": {"primary_db": "db", "cdm_schema": "omop"}},
        )
        out = tmp_path / "config.toml"
        save_stack_config(cfg, out)
        data = tomllib.loads(out.read_text())
        assert "password" not in data["connections"]["db"]
        assert "host" not in data["connections"]["db"]

    def test_creates_parent_dirs(self, tmp_path):
        cfg = StackConfig.for_session()
        out = tmp_path / "nested" / "dirs" / "config.toml"
        save_stack_config(cfg, out)
        assert out.exists()


class TestPatchActiveProfile:
    def test_sets_profile_in_new_file(self, tmp_path):
        out = tmp_path / "config.toml"
        patch_active_profile("prod", out)
        data = tomllib.loads(out.read_text())
        assert data["active_profile"] == "prod"

    def test_updates_existing_file(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={"db": {"dialect": "sqlite"}},
            resources={"default": {"primary_db": "db", "cdm_schema": "omop"}},
        )
        out = tmp_path / "config.toml"
        save_stack_config(cfg, out)

        patch_active_profile("test", out)
        data = tomllib.loads(out.read_text())
        assert data["active_profile"] == "test"
        # other data preserved
        assert "connections" in data

    def test_does_not_touch_other_fields(self, tmp_path):
        cfg = StackConfig.for_session(
            connections={"db": {"dialect": "sqlite"}},
            resources={"default": {"primary_db": "db", "cdm_schema": "omop"}},
            active_profile="dev",
        )
        out = tmp_path / "config.toml"
        save_stack_config(cfg, out)

        patch_active_profile("prod", out)
        data = tomllib.loads(out.read_text())
        assert data["active_profile"] == "prod"
        assert data["connections"]["db"]["dialect"] == "sqlite"
