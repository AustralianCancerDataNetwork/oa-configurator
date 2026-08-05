"""Public package surface for ``oa-configurator``."""

from .io import FLAT_ENV_PATH, save_stack_config, write_env_file
from .loader import DEFAULT_CONFIG_PATH, load_stack_config
from .logging_config import LoggingConfig, RedactingFormatter, configure_logging, get_logger
from .stack_config import (
    ConnectionConfig,
    DatabaseConfig,
    ModelConfig,
    ProviderConfig,
    RefTo,
    Sensitive,
    StackConfig,
    VectorStoreConfig,
)
from .package_base import ConfigurationError, PackageConfigBase
from .resolver import (
    Resolver,
    ResolvedConnection,
    ResolvedDatabase,
    ResolvedModel,
    ResolvedProvider,
    ResolvedToolConfig,
    ResolvedVectorStore,
    Role,
)

__all__ = [
    "ConfigurationError",
    "ConnectionConfig",
    "DEFAULT_CONFIG_PATH",
    "FLAT_ENV_PATH",
    "DatabaseConfig",
    "LoggingConfig",
    "ModelConfig",
    "RedactingFormatter",
    "PackageConfigBase",
    "ProviderConfig",
    "RefTo",
    "Resolver",
    "ResolvedConnection",
    "ResolvedDatabase",
    "ResolvedModel",
    "ResolvedProvider",
    "ResolvedToolConfig",
    "ResolvedVectorStore",
    "Role",
    "Sensitive",
    "StackConfig",
    "VectorStoreConfig",
    "configure_logging",
    "get_logger",
    "load_stack_config",
    "save_stack_config",
    "write_env_file",
]
