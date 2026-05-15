"""Resolution helpers that turn logical names into concrete typed handles."""

from __future__ import annotations

from dataclasses import dataclass
from keyword import iskeyword
from pathlib import Path
from typing import Callable, Generic, TypeVar

from .models import (
    ConnectionConfig,
    ProfileConfig,
    ResourceConfig,
    ResourceOverrideConfig,
    StackConfig,
    ToolConfig,
    ToolOverrideConfig,
)
from .paths import display_path, resolve_filesystem_path

T = TypeVar("T")


@dataclass(frozen=True)
class ResolvedDatabaseTarget:
    """Resolved concrete connection with both raw and safe renderings."""

    name: str
    connection: ConnectionConfig
    url: str

    @property
    def database(self) -> str | None:
        """Return the configured database name, if any."""

        return self.connection.database

    @property
    def dialect(self) -> str:
        """Return the resolved connection dialect."""

        return self.connection.dialect

    @property
    def safe_url(self) -> str:
        """Return a password-redacted URL for display and debugging."""

        return self.connection.as_safe_url_resolved()

    def __repr__(self) -> str:
        return (
            "ResolvedDatabaseTarget("
            f"name={self.name!r}, "
            f"dialect={self.dialect!r}, "
            f"database={self.database!r}, "
            f"url={self.safe_url!r}"
            ")"
        )


@dataclass(frozen=True)
class ResolvedResource:
    """Resolved logical resource bundle with concrete DB targets and local paths."""

    name: str
    primary_db: ResolvedDatabaseTarget
    vocab_db: ResolvedDatabaseTarget
    results_db: ResolvedDatabaseTarget
    omop_schema: str | None
    vocab_schema: str | None
    results_schema: str | None
    athena_source_path: Path | None
    artifact_root: Path | None
    embedding_file_root: Path | None
    analytic_db_file_root: Path | None

    def __repr__(self) -> str:
        return (
            "ResolvedResource("
            f"name={self.name!r}, "
            f"primary_db={self.primary_db.name!r}, "
            f"vocab_db={self.vocab_db.name!r}, "
            f"results_db={self.results_db.name!r}, "
            f"omop_schema={self.omop_schema!r}, "
            f"vocab_schema={self.vocab_schema!r}, "
            f"results_schema={self.results_schema!r}, "
            f"embedding_file_root={display_path(self.embedding_file_root)!r}, "
            f"analytic_db_file_root={display_path(self.analytic_db_file_root)!r}"
            ")"
        )


@dataclass(frozen=True)
class ResolvedToolConfig:
    """Resolved tool defaults with local paths expanded for direct use."""

    name: str
    backend: str | None
    default_resource: str | None
    embedding_file_root: Path | None
    database_file_root: Path | None

    def __repr__(self) -> str:
        return (
            "ResolvedToolConfig("
            f"name={self.name!r}, "
            f"backend={self.backend!r}, "
            f"default_resource={self.default_resource!r}, "
            f"embedding_file_root={display_path(self.embedding_file_root)!r}, "
            f"database_file_root={display_path(self.database_file_root)!r}"
            ")"
        )


class _NameNamespace(Generic[T]):
    """Interactive namespace exposing non-sensitive names as attributes.

    This is deliberately small DX sugar for REPL and notebook use:

    - ``resolver.resources.default``
    - ``resolver.tools.omop_emb``
    - ``resolver.connections.prod_cdm``

    Only identifier-safe names are exposed as attributes. All names remain
    available via ``[]`` lookup.
    """

    def __init__(self, *, names: Callable[[], tuple[str, ...]], resolver: Callable[[str], T], label: str):
        self._names = names
        self._resolver = resolver
        self._label = label

    def __getitem__(self, name: str) -> T:
        """Resolve a configured item by its exact name."""

        return self._resolver(name)

    def __getattr__(self, name: str) -> T:
        """Resolve identifier-safe names as interactive attributes."""

        if name in self._names():
            return self._resolver(name)
        raise AttributeError(f"Unknown {self._label}: {name}")

    def __dir__(self) -> list[str]:
        """Expose safe names for tab completion without leaking secrets."""

        return sorted(name for name in self._names() if _is_identifier_safe(name))

    def __repr__(self) -> str:
        visible = ", ".join(self.__dir__())
        return f"<{self._label} [{visible}]>"


class Resolver:
    """Resolve logical names in :class:`StackConfig` into typed handles."""

    def __init__(self, config: StackConfig):
        """Create a resolver around one validated stack configuration."""

        self.config = config
        self.connections = _NameNamespace(
            names=self.connection_names,
            resolver=self.resolve_connection,
            label="connections",
        )
        self.resources = _NameNamespace(
            names=self.resource_names,
            resolver=self.resolve_resource,
            label="resources",
        )
        self.tools = _NameNamespace(
            names=self.tool_names,
            resolver=self.resolve_tool,
            label="tools",
        )

    def profile_names(self) -> tuple[str, ...]:
        """Return known profile names in sorted order."""

        return self.config.profile_names()

    def connection_names(self) -> tuple[str, ...]:
        """Return known connection names in sorted order."""

        return self.config.connection_names()

    def resource_names(self) -> tuple[str, ...]:
        """Return known resource names in sorted order."""

        return self.config.resource_names()

    def tool_names(self) -> tuple[str, ...]:
        """Return known tool names in sorted order."""

        return self.config.tool_names()

    def active_profile_name(self) -> str:
        """Return the name of the currently active profile."""

        return self.config.settings.active_profile

    def active_profile(self) -> ProfileConfig | None:
        """Return the active profile configuration, if one exists."""

        return self.config.active_profile_config()

    def complete_connection_name(self, prefix: str = "") -> tuple[str, ...]:
        """Return connection names matching a prefix for simple shell completion."""

        return tuple(name for name in self.connection_names() if name.startswith(prefix))

    def complete_resource_name(self, prefix: str = "") -> tuple[str, ...]:
        """Return resource names matching a prefix for simple shell completion."""

        return tuple(name for name in self.resource_names() if name.startswith(prefix))

    def complete_tool_name(self, prefix: str = "") -> tuple[str, ...]:
        """Return tool names matching a prefix for simple shell completion."""

        return tuple(name for name in self.tool_names() if name.startswith(prefix))

    @property
    def configuration_base_path(self) -> Path:
        """Return the resolved base directory for all relative filesystem paths."""

        return self.config.configuration_base_path

    def resolve_connection(self, name: str) -> ResolvedDatabaseTarget:
        """Resolve one configured connection name into a concrete connection target."""

        connection = _get_named_config(self.config.connections, kind="connection", name=name)
        return ResolvedDatabaseTarget(
            name=name,
            connection=connection,
            url=connection.as_url_resolved(self.configuration_base_path),
        )

    def resolve_resource(self, name: str) -> ResolvedResource:
        """Resolve one logical resource bundle into concrete database targets."""

        resource = _get_named_config(self.config.resources, kind="resource", name=name)
        resource = self._apply_resource_overlay(name, resource)

        primary = self.resolve_connection(resource.primary_db)
        vocab = self.resolve_connection(resource.vocab_db or resource.primary_db)
        results = self.resolve_connection(resource.results_db or resource.primary_db)

        return ResolvedResource(
            name=name,
            primary_db=primary,
            vocab_db=vocab,
            results_db=results,
            omop_schema=resource.omop_schema,
            vocab_schema=resource.vocab_schema,
            results_schema=resource.results_schema,
            athena_source_path=_resolve_optional_path(resource.athena_source_path, self.configuration_base_path),
            artifact_root=_resolve_optional_path(resource.artifact_root, self.configuration_base_path),
            embedding_file_root=_resolve_optional_path(resource.embedding_file_root, self.configuration_base_path),
            analytic_db_file_root=_resolve_optional_path(resource.analytic_db_file_root, self.configuration_base_path),
        )

    def resolve_tool(self, name: str) -> ResolvedToolConfig:
        """Resolve one tool-default entry into expanded settings."""

        tool = _get_named_config(self.config.tools, kind="tool", name=name)
        tool = self._apply_tool_overlay(name, tool)

        return ResolvedToolConfig(
            name=name,
            backend=tool.backend,
            default_resource=tool.default_resource,
            embedding_file_root=_resolve_optional_path(tool.embedding_file_root, self.configuration_base_path),
            database_file_root=_resolve_optional_path(tool.database_file_root, self.configuration_base_path),
        )

    def _apply_resource_overlay(self, name: str, resource: ResourceConfig) -> ResourceConfig:
        """Apply the active profile's partial patch to a base resource."""

        profile = self.active_profile()
        if profile is None:
            return resource

        override = profile.resource_overrides.get(name)
        if override is None:
            return resource

        return _merge_resource_config(resource, override)

    def _apply_tool_overlay(self, name: str, tool: ToolConfig) -> ToolConfig:
        """Apply the active profile's partial patch to a base tool config."""

        profile = self.active_profile()
        if profile is None:
            return tool

        override = profile.tool_overrides.get(name)
        if override is None:
            return tool

        return _merge_tool_config(tool, override)

    def __repr__(self) -> str:
        return (
            "Resolver("
            f"profiles={len(self.config.profiles)}, "
            f"connections={len(self.config.connections)}, "
            f"resources={len(self.config.resources)}, "
            f"tools={len(self.config.tools)}"
            ")"
        )


def _resolve_optional_path(value: str | None, configuration_base_path: Path) -> Path | None:
    """Resolve an optional filesystem value against the configuration base path."""

    if value is None:
        return None
    return resolve_filesystem_path(value, configuration_base_path)


def _is_identifier_safe(name: str) -> bool:
    """Return whether a configured name is safe to expose as an attribute."""

    return name.isidentifier() and not iskeyword(name)


def _get_named_config(mapping: dict[str, T], *, kind: str, name: str) -> T:
    """Return a named config object with a consistent missing-name error."""

    try:
        return mapping[name]
    except KeyError as exc:
        raise KeyError(f"Unknown {kind}: {name}") from exc


def _merge_resource_config(
    base: ResourceConfig,
    override: ResourceOverrideConfig,
) -> ResourceConfig:
    """Return a resource config with a partial override applied."""

    return base.model_copy(update=override.model_dump(exclude_none=True))


def _merge_tool_config(
    base: ToolConfig,
    override: ToolOverrideConfig,
) -> ToolConfig:
    """Return a tool config with a partial override applied.

    ``extra`` is merged shallowly so an override can add or replace individual
    keys without discarding the full base mapping.
    """

    update = override.model_dump(exclude_none=True)
    if "extra" in update:
        update["extra"] = {
            **base.extra,
            **(update["extra"] or {}),
        }
    return base.model_copy(update=update)
