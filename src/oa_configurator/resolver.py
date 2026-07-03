"""Resolution helpers: turn logical config names into typed, usable handles."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from sqlalchemy.engine import Engine

from .models import (
    DatabaseConfig,
    KnowledgeKind,
    KnowledgeResourceConfig,
    KnowledgeResourceMap,
    LocalPathKnowledgeResource,
    ProfileOverrideConfig,
    ResourceConfig,
    StackConfig,
    ToolConfig,
)

T = TypeVar("T")
logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ResolvedResource:
    """Resolved logical resource with concrete DB configs and effective schema names.

    Attributes
    ----------
    name : str
        Logical name of the resource as declared in the config or an alias.
    database : DatabaseConfig
        Resolved primary database config for this resource. The ``name`` field
        on the config is set to the logical database name.
    vocab_database : DatabaseConfig
        Resolved vocabulary database config. May be the same object as
        *database* if no separate vocab database is configured.
    cdm_schema : str
        Effective CDM schema name for this resource.
    vocab_schema : str
        Effective vocabulary schema name. May equal *cdm_schema* when no
        separate vocab schema is configured.
    results_schema : str | None
        Effective results schema name, or None if not configured.
    """

    name: str
    database: DatabaseConfig
    vocab_database: DatabaseConfig
    cdm_schema: str
    vocab_schema: str
    results_schema: str | None

    def database_target(self, role: Literal["primary", "vocab"] = "primary") -> DatabaseConfig:
        """Return the resolved database target for a given role.

        Parameters
        ----------
        role : {"primary", "vocab"}, optional
            Which database target to return. Defaults to ``"primary"``.
            When ``vocab_database`` was not configured, ``"vocab"`` returns the
            same target as ``"primary"``.

        Returns
        -------
        DatabaseConfig
            The database config for *role*, with ``name`` set.

        Raises
        ------
        ValueError
            If *role* is not ``"primary"`` or ``"vocab"``.
        """
        if role == "primary":
            return self.database
        if role == "vocab":
            return self.vocab_database
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
        """Create a SQLAlchemy engine with the schema translate map applied.

        The schema translate map routes OMOP ORM models to the correct schemas
        automatically (``None`` -> cdm_schema, ``"vocab"`` -> vocab_schema,
        ``"results"`` -> results_schema when configured).

        Parameters
        ----------
        role : {"primary", "vocab"}, optional
            Which database target to create an engine for. Defaults to
            ``"primary"``.
        execution_options : dict, optional
            Additional execution options merged into the engine. The
            ``schema_translate_map`` key is set automatically and must
            not be supplied here.
        **kwargs
            Forwarded to ``sqlalchemy.create_engine``.

        Returns
        -------
        sqlalchemy.engine.Engine
            Engine configured with ``schema_translate_map`` for OMOP ORM routing.
        """
        engine = self.database_target(role).create_engine(**kwargs)
        stm = self.schema_translate_map()
        merged_opts = dict(execution_options or {})
        merged_opts.setdefault("schema_translate_map", stm)
        return engine.execution_options(**merged_opts)

    def __repr__(self) -> str:
        return (
            f"ResolvedResource(name={self.name!r}, "
            f"database={self.database.name!r}, "
            f"cdm_schema={self.cdm_schema!r}, "
            f"vocab_schema={self.vocab_schema!r}, "
            f"results_schema={self.results_schema!r})"
        )


@dataclass(frozen=True)
class ResolvedToolConfig:
    """Resolved tool section with raw extra dict for PackageConfigBase consumption."""

    name: str
    default_resource: str | None
    default_knowledge_resource: str | None
    extra: dict[str, Any]

    def __repr__(self) -> str:
        return (
            f"ResolvedToolConfig(name={self.name!r}, "
            f"default_resource={self.default_resource!r}, "
            f"default_knowledge_resource={self.default_knowledge_resource!r}, "
            f"extra_keys={sorted(self.extra)!r})"
        )


@dataclass(frozen=True)
class ResolvedKnowledgeResource:
    """Base resolved handle for all knowledge resource kinds.

    Subclasses carry kind-specific fields. Use ``isinstance`` to narrow.
    """

    name: str
    kind: KnowledgeKind


@dataclass(frozen=True)
class ResolvedLocalPathKnowledgeResource(ResolvedKnowledgeResource):
    """Resolved handle for a :class:`~oa_configurator.LocalPathKnowledgeResource`.

    Attributes
    ----------
    root : Path
        Absolute path to the knowledge source root directory. Always absolute
        after resolution regardless of how ``root`` was specified in config.
    """

    root: Path

    @property
    def exists(self) -> bool:
        """Return True when the resolved root currently exists on disk."""
        return self.root.exists()

    def __repr__(self) -> str:
        return (
            f"ResolvedLocalPathKnowledgeResource(name={self.name!r}, root={str(self.root)!r})"
        )


class Resolver:
    """Resolves logical names in a StackConfig into typed, usable handles."""

    def __init__(self, config: StackConfig) -> None:
        self.config = config

    def resolve_database(self, name: str) -> DatabaseConfig:
        """Resolve a database name to its config with ``name`` set.

        Profile overlay databases take precedence over base databases.
        Returns the effective :class:`~oa_configurator.DatabaseConfig` with
        the logical *name* stored on the ``name`` field.
        """
        db = self.effective_database(name)
        resolved = db.model_copy(update={"name": name})
        logger.debug("Resolved database %r → %s", name, resolved.safe_url())
        return resolved

    def resolve_resource(self, name: str) -> ResolvedResource:
        """Resolve a resource name to a concrete bundle of DB targets and schemas.

        Applies profile overlays and resource aliases. The vocab DB falls back
        to the primary DB when not explicitly configured; the vocab schema falls
        back to the CDM schema under the same condition.

        Parameters
        ----------
        name : str
            Resource name or alias as declared in ``[resources]`` or
            ``[resource_aliases]``.

        Returns
        -------
        ResolvedResource
            Fully resolved resource with concrete database targets and
            effective schema names.

        Raises
        ------
        KeyError
            If *name* (after alias resolution) does not exist in the config.
        """
        resource = self._effective_resource(name)

        primary = self.resolve_database(resource.database)
        vocab = self.resolve_database(resource.vocab_database or resource.database)
        effective_vocab_schema = resource.vocab_schema or resource.cdm_schema

        resolved = ResolvedResource(
            name=name,
            database=primary,
            vocab_database=vocab,
            cdm_schema=resource.cdm_schema,
            vocab_schema=effective_vocab_schema,
            results_schema=resource.results_schema,
        )
        logger.debug(
            "Resolved resource %r → database=%s cdm_schema=%r",
            name,
            resolved.database.safe_url(),
            resolved.cdm_schema,
        )
        return resolved

    def resolve_tool(self, name: str) -> ResolvedToolConfig:
        """Resolve a tool name to its configuration.

        Parameters
        ----------
        name : str
            Tool name as declared in ``[tools]``.

        Returns
        -------
        ResolvedToolConfig
            Resolved config with the raw extra dict intact for consumption
            by ``PackageConfigBase.from_stack``.

        Raises
        ------
        KeyError
            If *name* does not exist in the config.
        """
        tool = self._effective_tool(name)
        resolved = ResolvedToolConfig(
            name=name,
            default_resource=tool.default_resource,
            default_knowledge_resource=tool.default_knowledge_resource,
            extra=dict(tool.extra),
        )
        logger.debug(
            "Resolved tool %r with default_resource=%r default_knowledge_resource=%r",
            name,
            resolved.default_resource,
            resolved.default_knowledge_resource,
        )
        return resolved

    def resolve_knowledge_resource(self, name: str) -> ResolvedKnowledgeResource:
        """Resolve a knowledge resource name to a kind-specific resolved handle.

        Applies profile overlays and knowledge-resource aliases. Dispatches on
        ``kind`` and returns the appropriate :class:`ResolvedKnowledgeResource`
        subclass. Add a new ``case`` here when a new kind is introduced.
        """
        config = self._effective_knowledge_resource(name)
        if isinstance(config, LocalPathKnowledgeResource):
            root = _resolve_path(config.root, self.config.loaded_path)
            resolved = ResolvedLocalPathKnowledgeResource(name=name, kind=config.kind, root=root)
            logger.debug(
                "Resolved knowledge resource %r → kind=%r root=%s",
                name, resolved.kind, resolved.root,
            )
            return resolved
        raise ValueError(
            f"Unknown knowledge resource kind {config.kind!r} for {name!r}. "
            "Add an isinstance branch in Resolver.resolve_knowledge_resource."
        )

    def with_overrides(
        self,
        *,
        databases: dict[str, DatabaseConfig] | None = None,
        resources: dict[str, ResourceConfig] | None = None,
        knowledge_resources: KnowledgeResourceMap | None = None,
        tools: dict[str, ToolConfig] | None = None,
        resource_aliases: dict[str, str] | None = None,
        knowledge_resource_aliases: dict[str, str] | None = None,
    ) -> Resolver:
        """Return a new Resolver with entries merged over the current config.

        Useful for session-level overrides without touching the TOML file.
        """
        new_config = StackConfig(
            active_profile=self.config.active_profile,
            profiles=self.config.profiles,
            databases={**self.config.databases, **(databases or {})},
            resources={**self.config.resources, **(resources or {})},
            knowledge_resources={**self.config.knowledge_resources, **(knowledge_resources or {})},  # type: ignore[arg-type]
            tools={**self.config.tools, **(tools or {})},
            resource_aliases={**self.config.resource_aliases, **(resource_aliases or {})},
            knowledge_resource_aliases={**self.config.knowledge_resource_aliases, **(knowledge_resource_aliases or {})},
            logging=self.config.logging,
        )
        if self.config.loaded_path is not None:
            new_config.bind_loaded_path(self.config.loaded_path)
        return Resolver(new_config)

    def database_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured database names."""
        return self.config.database_names()

    def resource_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured resource names."""
        return self.config.resource_names()

    def tool_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured tool names."""
        return self.config.tool_names()

    def knowledge_resource_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured knowledge resource names."""
        return self.config.knowledge_resource_names()

    def profile_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured profile names."""
        return self.config.profile_names()

    def active_profile_name(self) -> str | None:
        """Return the name of the currently active profile, or None."""
        return self.config.active_profile

    def _active_profile(self) -> ProfileOverrideConfig | None:
        if self.config.active_profile is None:
            return None
        return self.config.profiles.get(self.config.active_profile)

    def effective_database(self, name: str) -> DatabaseConfig:
        """Return the active DatabaseConfig for a database name.

        Profile overlay databases take precedence over base databases.
        Unlike ``resolve_database``, this returns the raw config object
        rather than a resolved target; useful when individual fields (host,
        port, etc.) are needed directly.

        Parameters
        ----------
        name : str
            Database name as it appears in ``[databases]`` or a profile overlay.

        Returns
        -------
        DatabaseConfig
            The effective database config for the active profile.

        Raises
        ------
        KeyError
            If *name* does not exist in the base config or the active profile.
        """
        profile = self._active_profile()
        if profile and name in profile.databases:
            return profile.databases[name]
        return _get_named(self.config.databases, "database", name)

    def _effective_resource(self, name: str) -> ResourceConfig:
        profile = self._active_profile()
        actual_name = self.config.resource_aliases.get(name, name)
        if profile and actual_name in profile.resources:
            return profile.resources[actual_name]
        return _get_named(self.config.resources, "resource", actual_name)

    def _effective_knowledge_resource(self, name: str) -> KnowledgeResourceConfig:
        profile = self._active_profile()
        actual_name = self.config.knowledge_resource_aliases.get(name, name)
        if profile and actual_name in profile.knowledge_resources:
            return profile.knowledge_resources[actual_name]
        return _get_named(self.config.knowledge_resources, "knowledge resource", actual_name)

    def _effective_tool(self, name: str) -> ToolConfig:
        profile = self._active_profile()
        if profile and name in profile.tools:
            return profile.tools[name]
        return _get_named(self.config.tools, "tool", name)

    @classmethod
    def from_active_config(cls) -> Resolver:
        """Create a Resolver from the currently active stack config file."""
        from .loader import load_stack_config
        return cls(load_stack_config())

    def __repr__(self) -> str:
        return (
            f"Resolver(active_profile={self.config.active_profile!r}, "
            f"databases={len(self.config.databases)}, "
            f"resources={len(self.config.resources)}, "
            f"knowledge_resources={len(self.config.knowledge_resources)}, "
            f"tools={len(self.config.tools)})"
        )


def _get_named(mapping: dict[str, T], kind: str, name: str) -> T:
    try:
        return mapping[name]
    except KeyError as exc:
        raise KeyError(f"Unknown {kind}: {name!r}") from exc


def _resolve_path(path: Path, loaded_path: Path | None) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    if loaded_path is not None:
        return (loaded_path.parent / expanded).resolve()
    logger.warning(
        "Resolving relative knowledge resource path %r against process CWD because no config file "
        "path is set. Call bind_loaded_path() or use an absolute path to avoid CWD-dependent results.",
        str(path),
    )
    return expanded.resolve()
