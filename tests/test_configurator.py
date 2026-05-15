"""Regression tests for configuration loading and resolution."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from oa_configurator.loader import load_stack_config
from oa_configurator.models import ConnectionConfig, ProfileConfig, ResourceConfig, StackConfig
from oa_configurator.resolver import Resolver


class ConnectionConfigTests(unittest.TestCase):
    def test_resolves_relative_sqlite_path_from_configuration_base(self) -> None:
        base_path = Path("/tmp/config-root").resolve()
        connection = ConnectionConfig(kind="file", dialect="sqlite", path="data/example.sqlite")

        resolved = connection.as_url_resolved(base_path)

        self.assertEqual(resolved, f"sqlite:///{base_path / 'data/example.sqlite'}")

    def test_safe_url_redacts_password(self) -> None:
        connection = ConnectionConfig(
            dialect="postgresql",
            host="db.internal",
            user="omop",
            password="secret",
            database="cdm",
        )

        self.assertEqual(
            connection.as_safe_url(),
            "postgresql://omop:***@db.internal/cdm",
        )
        self.assertEqual(
            connection.as_url(),
            "postgresql://omop:secret@db.internal/cdm",
        )


class ResolverTests(unittest.TestCase):
    def test_active_profile_overrides_and_resolves_paths(self) -> None:
        config = StackConfig(
            connections={
                "local": ConnectionConfig(dialect="postgresql", database="local"),
                "prod": ConnectionConfig(dialect="postgresql", host="prod", database="prod"),
            },
            resources={
                "default": ResourceConfig(
                    primary_db="local",
                    artifact_root="artifacts/base",
                    embedding_file_root="artifacts/base/embeddings",
                )
            },
            profiles={
                "prod": ProfileConfig(
                    resource_overrides={
                        "default": {
                            "primary_db": "prod",
                            "artifact_root": "artifacts/prod",
                        }
                    }
                )
            },
        )
        config.settings.active_profile = "prod"
        config_root = Path("/tmp/config-dir").resolve()
        config.bind_loaded_path(config_root / "config.toml")

        resolved = Resolver(config).resolve_resource("default")

        self.assertEqual(resolved.primary_db.name, "prod")
        self.assertEqual(resolved.primary_db.url, "postgresql://prod/prod")
        self.assertEqual(resolved.artifact_root, config_root / "artifacts/prod")
        self.assertEqual(
            resolved.embedding_file_root,
            config_root / "artifacts/base/embeddings",
        )


class LoaderTests(unittest.TestCase):
    def test_environment_overrides_active_profile(self) -> None:
        config_text = """
[settings]
active_profile = "local"

[profiles.local]
description = "local"

[profiles.prod]
description = "prod"
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(config_text.strip(), encoding="utf-8")

            original = os.environ.get("OA_ACTIVE_PROFILE")
            try:
                os.environ["OA_ACTIVE_PROFILE"] = "prod"
                config = load_stack_config(config_path)
            finally:
                if original is None:
                    os.environ.pop("OA_ACTIVE_PROFILE", None)
                else:
                    os.environ["OA_ACTIVE_PROFILE"] = original

        self.assertEqual(config.settings.active_profile, "prod")
        self.assertEqual(config.configuration_base_path, config_path.parent.resolve())
