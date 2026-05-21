"""Resolution helpers: turn logical config names into typed, usable handles."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .models import (
    ConnectionConfig,
    ProfileOverrideConfig,
    ResourceConfig,
    StackConfig,
    ToolConfig,
)

T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedDatabaseTarget:
    """Concrete database connection ready for engine creation."""

    name: str
    url: str        # full URL including password — keep internal / do not log
    safe_url: str   # password redacted — safe for logs and display

    def create_engine(self, **kwargs: Any) -> Engine:
        """Create a SQLAlchemy engine for this connection."""
        if self.url.startswith("sqlite") and "read_only" in kwargs:
            kwargs.pop("read_only", None)
        return sa.create_engine(self.url, **kwargs)

    def __repr__(self) -> str:
        return f"ResolvedDatabaseTarget(name={self.name!r}, safe_url={self.safe_url!r})"


@dataclass(frozen=True)
class ResolvedResource:
    """Resolved logical resource with concrete DB targets and effective schema names."""

    name: str
    primary_db: ResolvedDatabaseTarget
    vocab_db: ResolvedDatabaseTarget       # equals primary_db when vocab_db not configured
    cdm_schema: str
    vocab_schema: str                      # equals cdm_schema when vocab_schema not set
    results_schema: str | None
    vocab_db_is_primary_fallback: bool

    def database_target(self, role: Literal["primary", "vocab"] = "primary") -> ResolvedDatabaseTarget:
        """Return the resolved target for a given role."""
        if role == "primary":
            return self.primary_db
        if role == "vocab":
            return self.vocab_db
        raise ValueError(f"Unknown role: {role!r}. Valid roles: 'primary', 'vocab'")

    def schema_translate_map(self) -> dict[str | None, str | None]:
        """SQLAlchemy schema translate map for OMOP ORM models.

        Maps:
          None      → cdm_schema  (default / unqualified tables → CDM)
          "vocab"   → vocab_schema (or cdm_schema as fallback)
          "results" → results_schema (omitted when not configured)
        """
        m: dict[str | None, str | None] = {
            None: self.cdm_schema,
            "vocab": self.vocab_schema,
        }
        if self.results_schema is not None:
            m["results"] = self.results_schema
        return m

    def create_engine(
        self,
        role: Literal["primary", "vocab"] = "primary",
        *,
        execution_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Engine:
        """Create an engine for a role with schema_translate_map applied."""
        engine = self.database_target(role).create_engine(**kwargs)
        stm = self.schema_translate_map()
        merged_opts = dict(execution_options or {})
        merged_opts.setdefault("schema_translate_map", stm)
        return engine.execution_options(**merged_opts)

    def __repr__(self) -> str:
        return (
            f"ResolvedResource(name={self.name!r}, "
            f"primary_db={self.primary_db.name!r}, "
            f"cdm_schema={self.cdm_schema!r}, "
            f"vocab_schema={self.vocab_schema!r}, "
            f"results_schema={self.results_schema!r})"
        )


@dataclass(frozen=True)
class ResolvedToolConfig:
    """Resolved tool section with raw extra dict for PackageConfigBase consumption."""

    name: str
    default_resource: str | None
    extra: dict[str, Any]

    def __repr__(self) -> str:
        return (
            f"ResolvedToolConfig(name={self.name!r}, "
            f"default_resource={self.default_resource!r}, "
            f"extra_keys={sorted(self.extra)!r})"
        )


class Resolver:
    """Resolves logical names in a StackConfig into typed, usable handles."""

    def __init__(self, config: StackConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Resolution methods
    # ------------------------------------------------------------------

    def resolve_connection(self, name: str) -> ResolvedDatabaseTarget:
        """Resolve a connection name to a concrete target.

        Profile overlay connections take precedence over base connections.
        """
        conn = self._effective_connection(name)
        target = ResolvedDatabaseTarget(
            name=name,
            url=conn.build_url(),
            safe_url=conn.safe_url(),
        )
        logger.debug("Resolved connection %r → %s", name, target.safe_url)
        return target

    def resolve_resource(self, name: str) -> ResolvedResource:
        """Resolve a resource name to a concrete bundle of DB targets and schemas."""
        resource = self._effective_resource(name)

        primary = self.resolve_connection(resource.primary_db)
        vocab_fallback = resource.vocab_db is None
        vocab = self.resolve_connection(resource.vocab_db or resource.primary_db)
        effective_vocab_schema = resource.vocab_schema or resource.cdm_schema

        resolved = ResolvedResource(
            name=name,
            primary_db=primary,
            vocab_db=vocab,
            cdm_schema=resource.cdm_schema,
            vocab_schema=effective_vocab_schema,
            results_schema=resource.results_schema,
            vocab_db_is_primary_fallback=vocab_fallback,
        )
        logger.debug(
            "Resolved resource %r → primary=%s cdm_schema=%r",
            name,
            resolved.primary_db.safe_url,
            resolved.cdm_schema,
        )
        return resolved

    def resolve_tool(self, name: str) -> ResolvedToolConfig:
        """Resolve a tool name to its configuration with extra dict intact."""
        tool = self._effective_tool(name)
        resolved = ResolvedToolConfig(
            name=name,
            default_resource=tool.default_resource,
            extra=dict(tool.extra),
        )
        logger.debug("Resolved tool %r with default_resource=%r", name, resolved.default_resource)
        return resolved

    def resolve_api_connection(self, name: str) -> ResolvedApiTarget:
        """Resolve one configured API connection name into a concrete API target.

        Secret sources are resolved on each call rather than memoized so
        callers can pick up environment or file rotations.
        """

        connection = _get_named_config(self.config.connections, kind="connection", name=name)
        if connection.kind != "api":
            raise ValueError(
                f"connection {name!r} has kind {connection.kind!r}, not 'api'; "
                "use resolve_connection() for database and file connections"
            )

        api_key: str | None = connection.api_key
        if connection.secret_source is not None:
            api_key = resolve_secret_value(
                connection.secret_source,
                configuration_base_path=self.configuration_base_path,
                secrets_dir=self.config.secrets_dir,
            )

        target = ResolvedApiTarget(
            name=name,
            base_url=connection.base_url,  # type: ignore[arg-type]  # guaranteed by validate_shape
            api_key=api_key,
            provider=connection.provider,
            safe_base_url=connection.base_url,  # type: ignore[arg-type]
        )
        logger.debug("Resolved api_connection %r to base_url=%s", name, target.safe_base_url)
        return target

    def with_overrides(
        self,
        *,
        connections: "dict[str, ConnectionConfig] | None" = None,
        resources: "dict[str, ResourceConfig] | None" = None,
        tools: "dict[str, ToolConfig] | None" = None,
    ) -> "Resolver":
        """Return a new Resolver with entries merged over the current config.

        Useful for session-level overrides without touching the TOML file.
        """
        new_config = StackConfig(
            active_profile=self.config.active_profile,
            profiles=self.config.profiles,
            connections={**self.config.connections, **(connections or {})},
            resources={**self.config.resources, **(resources or {})},
            tools={**self.config.tools, **(tools or {})},
            logging=self.config.logging,
        )
        if self.config.loaded_path is not None:
            new_config.bind_loaded_path(self.config.loaded_path)
        return Resolver(new_config)

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------

    def connection_names(self) -> tuple[str, ...]:
        return self.config.connection_names()

    def resource_names(self) -> tuple[str, ...]:
        return self.config.resource_names()

    def tool_names(self) -> tuple[str, ...]:
        return self.config.tool_names()

    def profile_names(self) -> tuple[str, ...]:
        return self.config.profile_names()

    def active_profile_name(self) -> str | None:
        return self.config.active_profile

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _active_profile(self) -> ProfileOverrideConfig | None:
        if self.config.active_profile is None:
            return None
        return self.config.profiles.get(self.config.active_profile)

    def _effective_connection(self, name: str) -> ConnectionConfig:
        profile = self._active_profile()
        if profile and name in profile.connections:
            return profile.connections[name]
        return _get_named(self.config.connections, "connection", name)

    def _effective_resource(self, name: str) -> ResourceConfig:
        profile = self._active_profile()
        if profile and name in profile.resources:
            return profile.resources[name]
        return _get_named(self.config.resources, "resource", name)

    def _effective_tool(self, name: str) -> ToolConfig:
        profile = self._active_profile()
        if profile and name in profile.tools:
            return profile.tools[name]
        return _get_named(self.config.tools, "tool", name)

    def __repr__(self) -> str:
        return (
            f"Resolver(active_profile={self.config.active_profile!r}, "
            f"connections={len(self.config.connections)}, "
            f"resources={len(self.config.resources)}, "
            f"tools={len(self.config.tools)})"
        )


def _get_named(mapping: dict[str, T], kind: str, name: str) -> T:
    try:
        return mapping[name]
    except KeyError as exc:
        raise KeyError(f"Unknown {kind}: {name!r}") from exc
