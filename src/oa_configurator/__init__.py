"""Public package surface for ``oa-configurator``."""

from .io import FLAT_ENV_PATH, patch_active_profile, save_stack_config, write_env_file
from .loader import DEFAULT_CONFIG_PATH, load_stack_config
from .logging_config import LoggingConfig, RedactingFormatter, configure_logging, get_logger
from .models import (
    DatabaseConfig,
    ProfileOverrideConfig,
    ResourceConfig,
    StackConfig,
    ToolConfig,
)
from .package_base import ConfigurationError, PackageConfigBase, ResourceSpec
from .resolver import Resolver, ResolvedDatabaseTarget, ResolvedResource, ResolvedToolConfig

from .pytest_plugin import (
    resolve_test_resource,
    ensure_test_db_exists,
    ensure_test_user_exists,
    ensure_db_extension_exists,
    create_fresh_test_db,
    drop_test_db,
)

__all__ = [
    "ConfigurationError",
    "DEFAULT_CONFIG_PATH",
    "FLAT_ENV_PATH",
    "DatabaseConfig",
    "LoggingConfig",
    "RedactingFormatter",
    "PackageConfigBase",
    "ResourceSpec",
    "ProfileOverrideConfig",
    "ResourceConfig",
    "Resolver",
    "ResolvedDatabaseTarget",
    "ResolvedResource",
    "ResolvedToolConfig",
    "StackConfig",
    "ToolConfig",
    "configure_logging",
    "get_logger",
    "load_stack_config",
    "patch_active_profile",
    "save_stack_config",
    "write_env_file",
    "resolve_test_resource",
    "ensure_test_db_exists",
    "ensure_test_user_exists",
    "ensure_db_extension_exists",
    "create_fresh_test_db",
    "drop_test_db",
]
