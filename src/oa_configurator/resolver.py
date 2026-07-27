"""Resolution helpers: turn logical config names into typed, usable handles."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .models import (
    DatabaseConfig,
    ModelConfig,
    ProfileOverrideConfig,
    ProviderConfig,
    ResourceConfig,
    StackConfig,
    ToolConfig,
)

T = TypeVar("T")
logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ResolvedDatabaseTarget:
    """Concrete database connection ready for engine creation.

    Attributes
    ----------
    name : str
        Logical name of the connection as declared in the config.
    url : str
        Full database URL including credentials.
        TODO: make this a private attribute to avoid accidental password exposure;
        requires a factory method or ``__post_init__`` since dataclass field
        visibility can't be changed without breaking callers.
    safe_url : str
        Database URL with credentials redacted, safe for logging and display.
    """

    name: str
    url: str
    safe_url: str

    def create_engine(self, **kwargs: Any) -> Engine:
        """Create a SQLAlchemy engine for this connection.

        Parameters
        ----------
        **kwargs
            Forwarded to ``sqlalchemy.create_engine``. The ``read_only``
            keyword is silently removed for SQLite connections, which do
            not support it.

        Returns
        -------
        sqlalchemy.engine.Engine
        """
        if self.url.startswith("sqlite") and "read_only" in kwargs:
            kwargs.pop("read_only", None)
        return sa.create_engine(self.url, **kwargs)

    def __repr__(self) -> str:
        return f"ResolvedDatabaseTarget(name={self.name!r}, safe_url={self.safe_url!r})"


@dataclass(frozen=True)
class ResolvedResource:
    """Resolved logical resource with concrete DB targets and effective schema names.
    
    Attributes
    ----------
    name : str
        Logical name of the resource as declared in the config or an alias.
    database : ResolvedDatabaseTarget
        Resolved primary database target for this resource.
    vocab_database : ResolvedDatabaseTarget
        Resolved vocabulary database target for this resource. May be the same as
        *database* if no separate vocab database is configured.
    cdm_schema : str
        Effective CDM schema name for this resource.
    vocab_schema : str
        Effective vocabulary schema name for this resource. May be the same as
        *cdm_schema* if no separate vocab schema is configured.
    results_schema : str | None
        Effective results schema name for this resource, or None if not configured.
    """

    name: str
    database: ResolvedDatabaseTarget
    vocab_database: ResolvedDatabaseTarget
    cdm_schema: str
    vocab_schema: str
    results_schema: str | None

    def database_target(self, role: Literal["primary", "vocab"] = "primary") -> ResolvedDatabaseTarget:
        """Return the resolved database target for a given role.

        Parameters
        ----------
        role : {"primary", "vocab"}, optional
            Which database target to return. Defaults to ``"primary"``.
            When ``vocab_database`` was not configured, ``"vocab"`` returns the
            same target as ``"primary"``.

        Returns
        -------
        ResolvedDatabaseTarget
            The concrete database target for *role*.

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
class ResolvedProvider:
    """Concrete LLM provider connection, ready to be served through.

    Attributes
    ----------
    name : str
        Logical name of the provider as declared in the config.
    provider : str
        Provider key, e.g. ``'ollama'``, ``'llamacpp'``, ``'openai'``.
    base_url : str, optional
        Resolved base URL for this deployment.
    api_key : str, optional
        Resolved API key for this deployment.
    """

    name: str
    provider: str
    base_url: str | None
    api_key: str | None

    def __repr__(self) -> str:
        return f"ResolvedProvider(name={self.name!r}, provider={self.provider!r})"


@dataclass(frozen=True)
class ResolvedModel:
    """Concrete, backend-agnostic model configuration.
    No explicit methods as it is just a data struct that can be used
    by the consuming package to construct a backend-specific model handle.

    Attributes
    ----------
    name : str
        Logical name of the model as declared in the config.
    provider : ResolvedProvider
        Resolved provider this model is served through.
    model : str
        Model name or identifier passed to the provider.
    configuration : dict[str, Any]
        Free-form per-model knobs (max_tokens, temperature, embedding_dim,
        and so on).
    """

    name: str
    provider: ResolvedProvider
    model: str
    configuration: dict[str, Any]

    def __repr__(self) -> str:
        return (
            f"ResolvedModel(name={self.name!r}, "
            f"provider={self.provider.provider!r}, model={self.model!r})"
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

    def resolve_database(self, name: str) -> ResolvedDatabaseTarget:
        """Resolve a database name to a concrete target.

        Profile overlay databases take precedence over base databases.
        """
        db = self.effective_database(name)
        target = ResolvedDatabaseTarget(
            name=name,
            url=db.build_url(),
            safe_url=db.safe_url(),
        )
        logger.debug("Resolved database %r → %s", name, target.safe_url)
        return target

    def resolve_provider(self, name: str) -> ResolvedProvider:
        """Resolve a provider name to a concrete connection.

        Profile overlay providers take precedence over base providers.

        Parameters
        ----------
        name : str
            Provider name as declared in ``[providers]`` or a profile overlay.

        Returns
        -------
        ResolvedProvider
            Resolved provider connection.

        Raises
        ------
        KeyError
            If *name* does not exist in the config.
        """
        provider = self.effective_provider(name)
        resolved = ResolvedProvider(
            name=name,
            provider=provider.provider,
            base_url=provider.base_url,
            api_key=provider.api_key,
        )
        logger.debug("Resolved provider %r → %s", name, resolved.provider)
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
            resolved.database.safe_url,
            resolved.cdm_schema,
        )
        return resolved

    def resolve_model(self, name: str) -> ResolvedModel:
        """Resolve a model name to a concrete, backend-ready configuration.

        Applies profile overlays. The unit consuming packages use directly:
        a package's own config just names an entry here (e.g.
        ``embedding_model = "embed-default"``), the same way
        ``default_resource`` names a :class:`~oa_configurator.models.ResourceConfig`
        entry.

        Parameters
        ----------
        name : str
            Model name as declared in ``[models]`` or a profile overlay.

        Returns
        -------
        ResolvedModel
            Fully resolved model with a concrete provider connection.

        Raises
        ------
        KeyError
            If *name* (or its provider) does not exist in the config.
        """
        model = self._effective_model(name)
        provider = self.resolve_provider(model.provider)

        resolved = ResolvedModel(
            name=name,
            provider=provider,
            model=model.model,
            configuration=dict(model.configuration),
        )
        logger.debug("Resolved model %r → provider=%s model=%r", name, provider.provider, resolved.model)
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
            extra=dict(tool.extra),
        )
        logger.debug("Resolved tool %r with default_resource=%r", name, resolved.default_resource)
        return resolved

    def with_overrides(
        self,
        *,
        databases: dict[str, DatabaseConfig] | None = None,
        resources: dict[str, ResourceConfig] | None = None,
        providers: dict[str, ProviderConfig] | None = None,
        models: dict[str, ModelConfig] | None = None,
        tools: dict[str, ToolConfig] | None = None,
    ) -> Resolver:
        """Return a new Resolver with entries merged over the current config.

        Useful for session-level overrides without touching the TOML file.
        """
        new_config = StackConfig(
            active_profile=self.config.active_profile,
            profiles=self.config.profiles,
            databases={**self.config.databases, **(databases or {})},
            resources={**self.config.resources, **(resources or {})},
            providers={**self.config.providers, **(providers or {})},
            models={**self.config.models, **(models or {})},
            tools={**self.config.tools, **(tools or {})},
            resource_aliases=self.config.resource_aliases,
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

    def provider_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured provider names."""
        return self.config.provider_names()

    def model_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured model names."""
        return self.config.model_names()

    def tool_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured tool names."""
        return self.config.tool_names()

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

    def effective_provider(self, name: str) -> ProviderConfig:
        """Return the active ProviderConfig for a provider name.

        Profile overlay providers take precedence over base providers.
        Unlike ``resolve_provider``, this returns the raw config object
        rather than a resolved target.

        Parameters
        ----------
        name : str
            Provider name as it appears in ``[providers]`` or a profile overlay.

        Returns
        -------
        ProviderConfig
            The effective provider config for the active profile.

        Raises
        ------
        KeyError
            If *name* does not exist in the base config or the active profile.
        """
        profile = self._active_profile()
        if profile and name in profile.providers:
            return profile.providers[name]
        return _get_named(self.config.providers, "provider", name)

    def _effective_model(self, name: str) -> ModelConfig:
        profile = self._active_profile()
        if profile and name in profile.models:
            return profile.models[name]
        return _get_named(self.config.models, "model", name)

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
            f"providers={len(self.config.providers)}, "
            f"models={len(self.config.models)}, "
            f"tools={len(self.config.tools)})"
        )


def _get_named(mapping: dict[str, T], kind: str, name: str) -> T:
    try:
        return mapping[name]
    except KeyError as exc:
        raise KeyError(f"Unknown {kind}: {name!r}") from exc
