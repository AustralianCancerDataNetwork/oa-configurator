"""Public package surface for ``oa-configurator``."""

from .cli_fields import (
    FieldSpec,
    FS_Database,
    FS_Model,
    FS_Provider,
    FS_Schema,
)
from .io import FLAT_ENV_PATH, patch_active_profile, save_stack_config, write_env_file
from .loader import DEFAULT_CONFIG_PATH, load_stack_config
from .logging_config import LoggingConfig, RedactingFormatter, configure_logging, get_logger
from .models import (
    DatabaseConfig,
    ModelConfig,
    ProfileOverrideConfig,
    ProviderConfig,
    ResourceConfig,
    StackConfig,
)
from .package_base import ConfigurationError, ModelFieldSpec, PackageConfigBase, ResourceRef, ResourceSpec
from .resolver import (
    Resolver,
    ResolvedDatabase,
    ResolvedModel,
    ResolvedProvider,
    ResolvedResource,
    ResolvedToolConfig,
)

__all__ = [
    "ConfigurationError",
    "DEFAULT_CONFIG_PATH",
    "FLAT_ENV_PATH",
    "FS_Database",
    "FS_Model",
    "FS_Provider",
    "FS_Schema",
    "FieldSpec",
    "DatabaseConfig",
    "LoggingConfig",
    "ModelConfig",
    "ModelFieldSpec",
    "RedactingFormatter",
    "PackageConfigBase",
    "ProviderConfig",
    "ResourceRef",
    "ResourceSpec",
    "ProfileOverrideConfig",
    "ResourceConfig",
    "Resolver",
    "ResolvedDatabase",
    "ResolvedModel",
    "ResolvedProvider",
    "ResolvedResource",
    "ResolvedToolConfig",
    "StackConfig",
    "configure_logging",
    "get_logger",
    "load_stack_config",
    "patch_active_profile",
    "save_stack_config",
    "write_env_file",
]
