"""Public package surface for ``oa-configurator``."""

from .cli_fields import (
    FieldSpec,
    FS_CDM_SCHEMA,
    FS_DATABASE,
    FS_DATABASE_NAME,
    FS_DIALECT,
    FS_HOST,
    FS_NON_CDM_SCHEMA,
    FS_PASSWORD,
    FS_PORT,
    FS_RESULTS_SCHEMA,
    FS_USER,
    FS_VOCAB_SCHEMA,
)
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

__all__ = [
    "ConfigurationError",
    "DEFAULT_CONFIG_PATH",
    "FLAT_ENV_PATH",
    "FS_CDM_SCHEMA",
    "FS_DATABASE",
    "FS_DATABASE_NAME",
    "FS_DIALECT",
    "FS_HOST",
    "FS_NON_CDM_SCHEMA",
    "FS_PASSWORD",
    "FS_PORT",
    "FS_RESULTS_SCHEMA",
    "FS_USER",
    "FS_VOCAB_SCHEMA",
    "FieldSpec",
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
]
