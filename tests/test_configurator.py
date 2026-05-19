"""Regression tests for configuration loading and resolution."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from oa_configurator.loader import load_stack_config
from oa_configurator.models import ConnectionConfig, ProfileConfig, ResourceConfig, StackConfig
from oa_configurator.resolver import Resolver
from oa_configurator.schema_helpers import schema_translate_map


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

    def test_create_engine_uses_resolved_connection_url(self) -> None:
        connection = ConnectionConfig(dialect="sqlite", database=":memory:")
        config = StackConfig(
            connections={
                "local": connection,
            },
        )
        config.bind_loaded_path(Path("/tmp/config-dir/config.toml").resolve())

        engine = Resolver(config).resolve_connection("local").create_engine()

        with engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT 1").scalar_one(), 1)

    def test_create_engine_uses_connection_engine_kwargs(self) -> None:
        connection = ConnectionConfig(
            dialect="sqlite",
            database=":memory:",
            engine_kwargs={"echo": True},
        )
        config = StackConfig(
            connections={
                "local": connection,
            },
        )
        config.bind_loaded_path(Path("/tmp/config-dir/config.toml").resolve())

        engine = Resolver(config).resolve_connection("local").create_engine()

        self.assertTrue(engine.echo)

    def test_password_and_secret_source_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "password or secret_source"):
            ConnectionConfig(
                dialect="postgresql",
                database="cdm",
                password="secret",
                secret_source="env:OA_DB_PASSWORD",
            )

    def test_as_url_requires_resolved_secret_context(self) -> None:
        connection = ConnectionConfig(
            dialect="postgresql",
            host="db.internal",
            user="omop",
            database="cdm",
            secret_source="env:OA_DB_PASSWORD",
        )

        with self.assertRaisesRegex(RuntimeError, "requires a resolved secret"):
            connection.as_url()

    def test_secret_source_safe_url_and_repr_do_not_expose_sentinel(self) -> None:
        connection = ConnectionConfig(
            dialect="postgresql",
            host="db.internal",
            user="omop",
            database="cdm",
            secret_source="env:OA_DB_PASSWORD",
        )

        self.assertEqual(connection.as_safe_url(), "postgresql://omop@db.internal/cdm")
        self.assertNotIn("_PASSWORD_UNSET", repr(connection))


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

    def test_resolves_file_backed_connection_secret_from_secrets_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_root = Path(tmpdir).resolve()
            secrets_dir = config_root / "secrets"
            secrets_dir.mkdir()
            (secrets_dir / "prod.password").write_text("super-secret\n", encoding="utf-8")

            config = StackConfig(
                settings={
                    "secrets_dir": "secrets",
                },
                connections={
                    "prod": ConnectionConfig(
                        dialect="postgresql",
                        host="db.internal",
                        user="omop",
                        database="cdm",
                        secret_source="file:prod.password",
                    )
                },
            )
            config.bind_loaded_path(config_root / "config.toml")

            resolved = Resolver(config).resolve_connection("prod")

        self.assertEqual(resolved.url, "postgresql://omop:super-secret@db.internal/cdm")
        self.assertEqual(resolved.safe_url, "postgresql://omop:***@db.internal/cdm")

    def test_resolves_env_backed_connection_secret(self) -> None:
        config = StackConfig(
            connections={
                "prod": ConnectionConfig(
                    dialect="postgresql",
                    host="db.internal",
                    user="omop",
                    database="cdm",
                    secret_source="env:OA_TEST_DB_PASSWORD",
                )
            },
        )
        config.bind_loaded_path(Path("/tmp/config-dir/config.toml").resolve())

        original = os.environ.get("OA_TEST_DB_PASSWORD")
        try:
            os.environ["OA_TEST_DB_PASSWORD"] = "env-secret"
            resolved = Resolver(config).resolve_connection("prod")
        finally:
            if original is None:
                os.environ.pop("OA_TEST_DB_PASSWORD", None)
            else:
                os.environ["OA_TEST_DB_PASSWORD"] = original

        self.assertEqual(resolved.url, "postgresql://omop:env-secret@db.internal/cdm")
        self.assertEqual(resolved.safe_url, "postgresql://omop:***@db.internal/cdm")

    def test_resolve_connection_keeps_inline_password_and_redacts_repr(self) -> None:
        config = StackConfig(
            connections={
                "local": ConnectionConfig(
                    dialect="postgresql",
                    host="localhost",
                    port=5432,
                    user="omop",
                    password="omop",
                    database="omop",
                )
            },
        )
        config.bind_loaded_path(Path("/tmp/config-dir/config.toml").resolve())

        resolved = Resolver(config).resolve_connection("local")

        self.assertEqual(resolved.url, "postgresql://omop:omop@localhost:5432/omop")
        self.assertEqual(resolved.safe_url, "postgresql://omop:***@localhost:5432/omop")
        self.assertEqual(
            repr(resolved),
            "ResolvedDatabaseTarget("
            "name='local', "
            "dialect='postgresql', "
            "database='omop', "
            "safe_url='postgresql://omop:***@localhost:5432/omop'"
            ")",
        )

    def test_resolved_resource_exposes_alias_flags_and_schema_translate_map(self) -> None:
        config = StackConfig(
            connections={
                "local": ConnectionConfig(dialect="sqlite", database=":memory:"),
            },
            resources={
                "default": ResourceConfig(
                    primary_db="local",
                    omop_schema="cdm",
                    vocab_schema="vocab",
                    results_schema="results",
                )
            },
        )
        config.bind_loaded_path(Path("/tmp/config-dir/config.toml").resolve())

        resolved = Resolver(config).resolve_resource("default")

        self.assertTrue(resolved.vocab_db_is_primary_fallback)
        self.assertTrue(resolved.results_db_is_primary_fallback)
        self.assertTrue(resolved.vocab_db_is_primary)
        self.assertTrue(resolved.results_db_is_primary)
        self.assertEqual(
            resolved.schema_translate_map(),
            {
                None: "cdm",
                "vocab": "vocab",
                "results": "results",
            },
        )
        self.assertEqual(schema_translate_map(resolved), resolved.schema_translate_map())

    def test_resolved_resource_create_engine_applies_schema_translate_map(self) -> None:
        config = StackConfig(
            connections={
                "local": ConnectionConfig(dialect="sqlite", database=":memory:"),
            },
            resources={
                "default": ResourceConfig(
                    primary_db="local",
                    omop_schema="cdm",
                    vocab_schema="vocab",
                )
            },
        )
        config.bind_loaded_path(Path("/tmp/config-dir/config.toml").resolve())

        engine = Resolver(config).resolve_resource("default").create_engine()

        self.assertEqual(
            engine.get_execution_options().get("schema_translate_map"),
            {
                None: "cdm",
                "vocab": "vocab",
            },
        )


class InlineConfigTests(unittest.TestCase):
    def test_for_session_resolves_without_toml_file(self) -> None:
        config = StackConfig.for_session(
            connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
            resources={"default": ResourceConfig(primary_db="local")},
        )

        engine = Resolver(config).resolve_resource("default").create_engine()

        with engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT 1").scalar_one(), 1)

    def test_for_session_uses_custom_base_path_for_relative_paths(self) -> None:
        base = Path("/tmp/session-base").resolve()
        config = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", path="data/db.sqlite")},
            resources={"default": ResourceConfig(primary_db="db")},
            base_path=base,
        )

        resolved = Resolver(config).resolve_connection("db")

        self.assertIn(str(base / "data/db.sqlite"), resolved.url)

    def test_for_session_configuration_base_path_defaults_to_cwd(self) -> None:
        config = StackConfig.for_session(
            connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
        )

        self.assertEqual(config.configuration_base_path, Path.cwd().resolve())

    def test_for_session_validates_references(self) -> None:
        with self.assertRaisesRegex(ValueError, "references unknown connection"):
            StackConfig.for_session(
                connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
                resources={"default": ResourceConfig(primary_db="missing")},
            )

    def test_with_overrides_adds_new_connection(self) -> None:
        base_config = StackConfig.for_session(
            connections={"prod": ConnectionConfig(dialect="postgresql", host="prod", database="cdm")},
            resources={"default": ResourceConfig(primary_db="prod")},
        )
        resolver = Resolver(base_config).with_overrides(
            connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
            resources={"default": ResourceConfig(primary_db="local")},
        )

        engine = resolver.resolve_resource("default").create_engine()

        with engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT 1").scalar_one(), 1)

    def test_with_overrides_preserves_unchanged_connections(self) -> None:
        base_config = StackConfig.for_session(
            connections={
                "prod": ConnectionConfig(dialect="postgresql", host="prod", database="cdm"),
                "vocab": ConnectionConfig(dialect="postgresql", host="vocab", database="vocab"),
            },
            resources={"default": ResourceConfig(primary_db="prod", vocab_db="vocab")},
        )
        resolver = Resolver(base_config).with_overrides(
            connections={"prod": ConnectionConfig(dialect="sqlite", database=":memory:")},
        )

        self.assertIn("vocab", resolver.connection_names())
        self.assertEqual(resolver.config.connections["vocab"].host, "vocab")

    def test_with_overrides_validates_references_in_merged_result(self) -> None:
        base_config = StackConfig.for_session(
            connections={"prod": ConnectionConfig(dialect="postgresql", host="prod", database="cdm")},
            resources={"default": ResourceConfig(primary_db="prod")},
        )
        with self.assertRaisesRegex(ValueError, "references unknown connection"):
            Resolver(base_config).with_overrides(
                resources={"default": ResourceConfig(primary_db="nonexistent")},
            )

    def test_with_overrides_preserves_path_context(self) -> None:
        base = Path("/tmp/override-base").resolve()
        base_config = StackConfig.for_session(
            connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
            base_path=base,
        )
        resolver = Resolver(base_config).with_overrides(
            resources={"default": ResourceConfig(primary_db="local", artifact_root="artifacts")},
        )

        resolved = resolver.resolve_resource("default")

        self.assertEqual(resolved.artifact_root, base / "artifacts")


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
        self.assertIsNone(config.secrets_dir)

    def test_settings_reject_removed_active_stack(self) -> None:
        with self.assertRaisesRegex(Exception, "active_stack"):
            StackConfig.model_validate(
                {
                    "settings": {
                        "active_profile": "local",
                        "active_stack": "default",
                    }
                }
            )

    def test_stack_config_rejects_unknown_resource_connection_reference(self) -> None:
        with self.assertRaisesRegex(ValueError, "resources.default.primary_db references unknown connection"):
            StackConfig(
                connections={
                    "local": ConnectionConfig(dialect="postgresql", database="omop"),
                },
                resources={
                    "default": ResourceConfig(primary_db="missing"),
                },
            )

    def test_stack_config_rejects_unknown_tool_resource_reference(self) -> None:
        with self.assertRaisesRegex(ValueError, "tools.omop_emb.default_resource references unknown resource"):
            StackConfig(
                tools={
                    "omop_emb": {
                        "default_resource": "missing",
                    }
                },
            )
