"""Regression tests for configuration loading and resolution."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import logging

from oa_configurator import SecretSourceResolutionError, SettingsConfig
from oa_configurator.loader import load_stack_config
from oa_configurator.logging_config import (
    LoggingConfig,
    LoggingHandlerConfig,
    STACK_LOG_NAMESPACES,
    configure_logging,
)
from oa_configurator.models import ConnectionConfig, ProfileConfig, ResourceConfig, StackConfig, ToolConfig
from oa_configurator.persistence import save_stack_config
from oa_configurator.resolver import Resolver
from oa_configurator.schema_helpers import schema_translate_map
from oa_configurator.secret_sources import resolve_secret_value


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

    def test_connection_config_rejects_unknown_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "Extra inputs are not permitted"):
            ConnectionConfig.model_validate(
                {
                    "dialect": "postgresql",
                    "database": "cdm",
                    "omop_shcema": "typo",
                }
            )

    def test_file_connection_requires_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "file connections require a path"):
            ConnectionConfig(
                kind="file",
                dialect="duckdb",
            )

    def test_file_connection_rejects_network_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "file connections may not define host"):
            ConnectionConfig(
                kind="file",
                dialect="duckdb",
                path="data/example.duckdb",
                host="localhost",
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

    @patch("oa_configurator.resolver.sa.create_engine")
    def test_read_only_postgresql_injects_engine_connect_args(self, mock_create_engine) -> None:
        connection = ConnectionConfig(
            dialect="postgresql",
            database="cdm",
            read_only=True,
            engine_kwargs={"pool_pre_ping": True},
        )
        config = StackConfig.for_session(
            connections={"prod": connection},
        )

        Resolver(config).resolve_connection("prod").create_engine()

        _, kwargs = mock_create_engine.call_args
        self.assertTrue(kwargs["pool_pre_ping"])
        self.assertIn("connect_args", kwargs)
        self.assertIn(
            "default_transaction_read_only=on",
            kwargs["connect_args"]["options"],
        )


class SecretSourceResolutionTests(unittest.TestCase):
    def test_root_exports_secret_error_and_settings_config(self) -> None:
        self.assertTrue(issubclass(SecretSourceResolutionError, RuntimeError))
        settings = SettingsConfig(active_profile="local")
        self.assertEqual(settings.active_profile, "local")

    def test_env_secret_happy_path(self) -> None:
        original = os.environ.get("OA_SECRET_TEST_VALUE")
        try:
            os.environ["OA_SECRET_TEST_VALUE"] = "secret"
            resolved = resolve_secret_value(
                "env:OA_SECRET_TEST_VALUE",
                configuration_base_path=Path("/tmp/config-root").resolve(),
            )
        finally:
            if original is None:
                os.environ.pop("OA_SECRET_TEST_VALUE", None)
            else:
                os.environ["OA_SECRET_TEST_VALUE"] = original

        self.assertEqual(resolved, "secret")

    def test_env_secret_raises_when_unset(self) -> None:
        original = os.environ.get("OA_SECRET_TEST_VALUE")
        try:
            os.environ.pop("OA_SECRET_TEST_VALUE", None)
            with self.assertRaisesRegex(SecretSourceResolutionError, "is not set"):
                resolve_secret_value(
                    "env:OA_SECRET_TEST_VALUE",
                    configuration_base_path=Path("/tmp/config-root").resolve(),
                )
        finally:
            if original is None:
                os.environ.pop("OA_SECRET_TEST_VALUE", None)
            else:
                os.environ["OA_SECRET_TEST_VALUE"] = original

    def test_env_secret_raises_when_empty(self) -> None:
        original = os.environ.get("OA_SECRET_TEST_VALUE")
        try:
            os.environ["OA_SECRET_TEST_VALUE"] = ""
            with self.assertRaisesRegex(SecretSourceResolutionError, "set but empty"):
                resolve_secret_value(
                    "env:OA_SECRET_TEST_VALUE",
                    configuration_base_path=Path("/tmp/config-root").resolve(),
                )
        finally:
            if original is None:
                os.environ.pop("OA_SECRET_TEST_VALUE", None)
            else:
                os.environ["OA_SECRET_TEST_VALUE"] = original

    def test_file_secret_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir).resolve()
            secret_path = base_path / "secrets" / "db.password"
            secret_path.parent.mkdir()
            secret_path.write_text("secret\n", encoding="utf-8")

            resolved = resolve_secret_value(
                "file:db.password",
                configuration_base_path=base_path,
                secrets_dir=secret_path.parent,
            )

        self.assertEqual(resolved, "secret")

    def test_file_secret_raises_when_missing(self) -> None:
        with self.assertRaisesRegex(SecretSourceResolutionError, "secret file not found"):
            resolve_secret_value(
                "file:missing.password",
                configuration_base_path=Path("/tmp/config-root").resolve(),
            )

    def test_file_secret_raises_when_path_is_not_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir).resolve()
            not_a_file = base_path / "secrets"
            not_a_file.mkdir()

            with self.assertRaisesRegex(SecretSourceResolutionError, "not a file"):
                resolve_secret_value(
                    "file:secrets",
                    configuration_base_path=base_path,
                )

    def test_unsupported_secret_kind_raises(self) -> None:
        with self.assertRaisesRegex(SecretSourceResolutionError, "unsupported secret source kind"):
            resolve_secret_value(
                "vault:path/to/secret",
                configuration_base_path=Path("/tmp/config-root").resolve(),
            )


class ResolverTests(unittest.TestCase):
    def test_resolve_connection_raises_for_unknown_name(self) -> None:
        config = StackConfig(
            connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
        )
        config.bind_loaded_path(Path("/tmp/config-dir/config.toml").resolve())

        with self.assertRaisesRegex(KeyError, "Unknown connection: missing"):
            Resolver(config).resolve_connection("missing")

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

    def test_with_overrides_preserves_logging_config(self) -> None:
        base_config = StackConfig.for_session(
            connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
        )
        base_config.logging.preset = "production"

        resolver = Resolver(base_config).with_overrides(
            resources={"default": ResourceConfig(primary_db="local")},
        )

        self.assertEqual(resolver.config.logging.preset, "production")

    def test_with_overrides_preserves_profile_overlays(self) -> None:
        base_config = StackConfig(
            settings={"active_profile": "prod"},
            profiles={
                "prod": ProfileConfig(
                    resource_overrides={
                        "default": {
                            "primary_db": "prod",
                        }
                    }
                )
            },
            connections={
                "local": ConnectionConfig(dialect="sqlite", database=":memory:"),
                "prod": ConnectionConfig(dialect="postgresql", host="prod", database="prod"),
            },
            resources={"default": ResourceConfig(primary_db="local")},
        )
        base_config.bind_loaded_path(Path("/tmp/config-dir/config.toml").resolve())

        resolver = Resolver(base_config).with_overrides(
            connections={"prod": ConnectionConfig(dialect="sqlite", database=":memory:")},
        )

        resolved = resolver.resolve_resource("default")

        self.assertEqual(resolved.primary_db.name, "prod")
        self.assertEqual(resolved.primary_db.url, "sqlite:///:memory:")

    def test_namespace_private_attribute_lookup_raises_attribute_error(self) -> None:
        resolver = Resolver(
            StackConfig.for_session(
                connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
            )
        )

        with self.assertRaises(AttributeError):
            getattr(resolver.connections, "_missing")

    def test_dynamic_dir_includes_public_dataclass_fields_and_methods(self) -> None:
        resolver = Resolver(
            StackConfig.for_session(
                connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
                resources={"default": ResourceConfig(primary_db="local")},
            )
        )

        resource_dir = dir(resolver.resolve_resource("default"))
        connection_dir = dir(resolver.resolve_connection("local"))

        self.assertIn("vocab_db_is_primary_fallback", resource_dir)
        self.assertIn("create_engine", resource_dir)
        self.assertIn("safe_url", connection_dir)
        self.assertNotIn("vocab_db_is_primary", resource_dir)


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

    def test_stack_config_rejects_unknown_top_level_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "Extra inputs are not permitted"):
            StackConfig.model_validate(
                {
                    "settings": {"active_profile": "local"},
                    "stacks": {},
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

    def test_profile_resource_override_rejects_unknown_connection_reference(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "profiles.prod.resource_overrides.default.primary_db references unknown connection",
        ):
            StackConfig(
                profiles={
                    "prod": ProfileConfig(
                        resource_overrides={
                            "default": {
                                "primary_db": "missing",
                            }
                        }
                    )
                },
                connections={
                    "local": ConnectionConfig(dialect="sqlite", database=":memory:"),
                },
                resources={
                    "default": ResourceConfig(primary_db="local"),
                },
            )

    def test_profile_tool_override_rejects_unknown_resource_reference(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "profiles.prod.tool_overrides.omop_emb.default_resource references unknown resource",
        ):
            StackConfig(
                profiles={
                    "prod": ProfileConfig(
                        tool_overrides={
                            "omop_emb": {
                                "default_resource": "missing",
                            }
                        }
                    )
                },
                resources={
                    "default": ResourceConfig(primary_db="local"),
                },
                connections={
                    "local": ConnectionConfig(dialect="sqlite", database=":memory:"),
                },
                tools={
                    "omop_emb": ToolConfig(default_resource="default"),
                },
            )

    def test_for_session_binds_base_path_for_relative_resolution(self) -> None:
        config = StackConfig.for_session(
            connections={
                "local": ConnectionConfig(dialect="sqlite", path="data/example.sqlite"),
            },
            resources={
                "default": ResourceConfig(primary_db="local"),
            },
            base_path=Path("/tmp/session-root"),
        )

        resolved = Resolver(config).resolve_connection("local")

        self.assertEqual(
            resolved.url,
            f"sqlite:///{Path('/tmp/session-root').resolve() / 'data/example.sqlite'}",
        )

    def test_load_stack_config_wraps_malformed_toml_with_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "broken.toml"
            config_path.write_text("[settings\nactive_profile = 'local'\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, str(config_path)):
                load_stack_config(config_path)


class PersistenceTests(unittest.TestCase):
    def test_save_stack_config_omits_default_logging_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            config = StackConfig(
                connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
                resources={"default": ResourceConfig(primary_db="local")},
            )

            save_stack_config(config, path)
            written = path.read_text(encoding="utf-8")

        self.assertNotIn("[logging]", written)

    def test_save_and_load_round_trip_preserves_basic_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            config = StackConfig(
                settings={"active_profile": "prod"},
                profiles={"prod": ProfileConfig(description="production")},
                connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
                resources={"default": ResourceConfig(primary_db="local", omop_schema="cdm")},
            )

            save_stack_config(config, path)
            loaded = load_stack_config(path)

        self.assertEqual(loaded.settings.active_profile, "prod")
        self.assertEqual(loaded.resources["default"].omop_schema, "cdm")
        self.assertIn("local", loaded.connections)


class _SavedLoggerState:
    """Context manager that saves and restores logger state for test isolation."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self._names = names
        self._saved: dict[str, tuple] = {}

    def __enter__(self) -> "_SavedLoggerState":
        for name in self._names:
            lg = logging.getLogger(name)
            self._saved[name] = (lg.level, lg.handlers[:], lg.propagate)
        return self

    def __exit__(self, *_: object) -> None:
        for name, (level, handlers, propagate) in self._saved.items():
            lg = logging.getLogger(name)
            lg.setLevel(level)
            lg.handlers = handlers
            lg.propagate = propagate


_ALL_TEST_NAMESPACES = STACK_LOG_NAMESPACES + ("sqlalchemy.engine",)


class LoggingConfigTests(unittest.TestCase):
    def test_library_preset_sets_warning_level_on_stack_namespaces(self) -> None:
        with _SavedLoggerState(_ALL_TEST_NAMESPACES):
            configure_logging(preset="library")
            for ns in STACK_LOG_NAMESPACES:
                self.assertEqual(logging.getLogger(ns).level, logging.WARNING)

    def test_notebook_preset_sets_info_level_and_adds_stdout_handler(self) -> None:
        import sys
        with _SavedLoggerState(_ALL_TEST_NAMESPACES):
            configure_logging(preset="notebook")
            for ns in STACK_LOG_NAMESPACES:
                lg = logging.getLogger(ns)
                self.assertEqual(lg.level, logging.INFO)
                stream_handlers = [
                    h for h in lg.handlers if isinstance(h, logging.StreamHandler)
                    and not isinstance(h, logging.FileHandler)
                    and h.stream is sys.stdout
                ]
                self.assertTrue(stream_handlers, f"{ns} should have a stdout handler")

    def test_application_preset_sets_info_level_and_adds_stderr_handler(self) -> None:
        import sys
        with _SavedLoggerState(_ALL_TEST_NAMESPACES):
            configure_logging(preset="application")
            lg = logging.getLogger("orm_loader")
            self.assertEqual(lg.level, logging.INFO)
            stream_handlers = [
                h for h in lg.handlers if isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
                and h.stream is sys.stderr
            ]
            self.assertTrue(stream_handlers)

    def test_library_preset_adds_no_handler(self) -> None:
        with _SavedLoggerState(_ALL_TEST_NAMESPACES):
            configure_logging(preset="library")
            for ns in STACK_LOG_NAMESPACES:
                non_null = [
                    h for h in logging.getLogger(ns).handlers
                    if not isinstance(h, logging.NullHandler)
                ]
                self.assertEqual(non_null, [], f"{ns} should have no non-null handlers")

    def test_level_override_supersedes_preset_default(self) -> None:
        with _SavedLoggerState(_ALL_TEST_NAMESPACES):
            configure_logging(LoggingConfig(preset="notebook", level="DEBUG"))
            for ns in STACK_LOG_NAMESPACES:
                self.assertEqual(logging.getLogger(ns).level, logging.DEBUG)

    def test_per_logger_overrides_applied(self) -> None:
        with _SavedLoggerState(_ALL_TEST_NAMESPACES):
            configure_logging(LoggingConfig(
                preset="library",
                loggers={"sqlalchemy.engine": "WARNING"},
            ))
            self.assertEqual(logging.getLogger("sqlalchemy.engine").level, logging.WARNING)

    def test_preset_shorthand_raises_when_config_also_provided(self) -> None:
        with self.assertRaises(TypeError):
            configure_logging(LoggingConfig(), preset="notebook")

    def test_configure_logging_from_stack_config_extracts_logging_block(self) -> None:
        config = StackConfig.for_session()
        config.logging = LoggingConfig(preset="library", level="ERROR")
        with _SavedLoggerState(_ALL_TEST_NAMESPACES):
            configure_logging(config)
            for ns in STACK_LOG_NAMESPACES:
                self.assertEqual(logging.getLogger(ns).level, logging.ERROR)

    def test_configure_logging_is_idempotent(self) -> None:
        with _SavedLoggerState(_ALL_TEST_NAMESPACES):
            configure_logging(preset="library")
            configure_logging(preset="library")
            for ns in STACK_LOG_NAMESPACES:
                self.assertEqual(logging.getLogger(ns).level, logging.WARNING)

    def test_invalid_level_raises(self) -> None:
        with self.assertRaises(ValueError):
            LoggingConfig(level="NONSENSE")

    def test_invalid_numeric_level_raises(self) -> None:
        with self.assertRaises(ValueError):
            LoggingConfig(level="99")

    def test_invalid_logger_level_raises(self) -> None:
        with self.assertRaises(ValueError):
            LoggingConfig(loggers={"sqlalchemy.engine": "NONSENSE"})

    def test_stack_config_logging_field_defaults_to_library(self) -> None:
        config = StackConfig()
        self.assertEqual(config.logging.preset, "library")

    def test_stack_config_logging_field_loaded_from_toml(self) -> None:
        config = StackConfig.model_validate({
            "logging": {
                "preset": "notebook",
                "level": "DEBUG",
            }
        })
        self.assertEqual(config.logging.preset, "notebook")
        self.assertEqual(config.logging.level, "DEBUG")

    def test_file_handler_created_at_given_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "app.log"
            config = LoggingConfig(
                preset="library",
                handler=LoggingHandlerConfig(target="file", file_path=str(log_path)),
            )
            with _SavedLoggerState(_ALL_TEST_NAMESPACES):
                configure_logging(config)
                logging.getLogger("orm_loader").warning("test message")
                # flush and close file handlers so content is written
                for ns in STACK_LOG_NAMESPACES:
                    for h in logging.getLogger(ns).handlers:
                        h.flush()
                        if isinstance(h, logging.FileHandler):
                            h.close()
            self.assertTrue(log_path.exists())
            self.assertIn("test message", log_path.read_text())

    def test_json_formatter_redacts_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "app.jsonl"
            config = LoggingConfig(
                preset="production",
                handler=LoggingHandlerConfig(target="file", format="json", file_path=str(log_path)),
            )
            with _SavedLoggerState(_ALL_TEST_NAMESPACES):
                configure_logging(config)
                logging.getLogger("oa_configurator").warning("password=secret url=postgresql://user:secret@host/db")
                for ns in STACK_LOG_NAMESPACES:
                    for h in logging.getLogger(ns).handlers:
                        h.flush()
                        if isinstance(h, logging.FileHandler):
                            h.close()

            content = log_path.read_text(encoding="utf-8")

        self.assertIn("password=<REDACTED>", content)
        self.assertIn("url=<REDACTED>", content)
        self.assertNotIn("password=secret", content)

    def test_json_formatter_redacts_exception_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "app.jsonl"
            config = LoggingConfig(
                preset="production",
                handler=LoggingHandlerConfig(target="file", format="json", file_path=str(log_path)),
            )
            with _SavedLoggerState(_ALL_TEST_NAMESPACES):
                configure_logging(config)
                try:
                    raise RuntimeError("password=secret")
                except RuntimeError:
                    logging.getLogger("oa_configurator").exception("failure")
                for ns in STACK_LOG_NAMESPACES:
                    for h in logging.getLogger(ns).handlers:
                        h.flush()
                        if isinstance(h, logging.FileHandler):
                            h.close()

            content = log_path.read_text(encoding="utf-8")

        self.assertIn("password=<REDACTED>", content)
        self.assertNotIn("password=secret", content)

    def test_configure_logging_rejects_unknown_type(self) -> None:
        with self.assertRaises(TypeError):
            configure_logging(object())

    def test_reconfigure_closes_old_handlers_and_clears_them_in_library_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            first_path = Path(tmpdir) / "first.log"
            second_path = Path(tmpdir) / "second.log"
            first_config = LoggingConfig(
                preset="application",
                handler=LoggingHandlerConfig(target="file", file_path=str(first_path)),
            )
            second_config = LoggingConfig(
                preset="application",
                handler=LoggingHandlerConfig(target="file", file_path=str(second_path)),
            )

            with _SavedLoggerState(_ALL_TEST_NAMESPACES):
                configure_logging(first_config)
                first_handler = next(
                    h
                    for h in logging.getLogger("oa_configurator").handlers
                    if isinstance(h, logging.FileHandler)
                )

                configure_logging(second_config)
                self.assertTrue(
                    first_handler.stream is None or first_handler.stream.closed
                )

                configure_logging(preset="library")
                for ns in STACK_LOG_NAMESPACES:
                    non_null = [
                        h for h in logging.getLogger(ns).handlers
                        if not isinstance(h, logging.NullHandler)
                    ]
                    self.assertEqual(non_null, [], f"{ns} should have no non-null handlers")
