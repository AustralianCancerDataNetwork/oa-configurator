"""Typed Pydantic models for the OMOP stack configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from sqlalchemy.engine import URL

from .logging_config import LoggingConfig


class DatabaseConfig(BaseModel):
    """Complete specification of one named database: server address, credentials, and target database.

    Referenced by :attr:`ResourceConfig.database` and
    :attr:`ResourceConfig.vocab_database`. Each entry under ``[databases]``
    in ``config.toml`` maps to one instance of this model.

    Passwords are stored in plaintext for now; secret management support
    is planned for a future release.
    """

    model_config = ConfigDict(extra="forbid")

    dialect: str = Field(
        description="SQLAlchemy dialect string, e.g. 'postgresql+psycopg', 'mssql+pyodbc', 'sqlite'."
    )
    host: str | None = Field(default=None, description="Hostname or IP address.")
    port: int | None = Field(default=None, description="Port number.")
    user: str | None = Field(default=None, description="Database username.")
    password: str | None = Field(
        default=None,
        description="Plaintext password. Secret management support is planned for a future release.",
    )
    database_name: str | None = Field(
        default=None,
        description="Database name on the server. For SQLite use ':memory:' or an absolute path.",
    )
    read_only: bool = Field(
        default=False,
        description="Hint only; enforcement depends on the dialect.",
    )
    test_only: bool = Field(
        default=False,
        description=(
            "Marks this connection as intended for testing only. "
            "It will be excluded from production resource prompts and "
            "used as a safety check to prevent accidental test operations "
            "on production data."
        ),
    )

    def to_env_pairs(self, prefix: str) -> list[str]:
        """Return ``PREFIX_FIELD=value`` strings for each non-None field.

        Used by :func:`~oa_configurator.io.write_env_file` to emit env vars for
        Docker Compose ``env_file:``. Field names are uppercased directly
        (e.g. ``host`` → ``PREFIX_HOST``), so adding a new field here
        automatically appears in the export without touching ``io.py``.
        Config-only flags (``read_only``, ``test_only``) are excluded — they
        are not database connection parameters.
        """
        return [
            f"{prefix}_{k.upper()}={v}"
            for k, v in self.model_dump().items()
            if v is not None and k not in {"read_only", "test_only"}
        ]

    def _build_url(self, hide_password: bool) -> str:
        if self.dialect.startswith("sqlite"):
            db = self.database_name or ":memory:"
            return f"sqlite:///{db}"
        if not self.host:
            raise ValueError(
                "DatabaseConfig has no `host` set and no longer defaults to 'localhost'."
                " Set `host` explicitly in config.toml."
            )
        return URL.create(
            drivername=self.dialect,
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database_name or "",
        ).render_as_string(hide_password=hide_password)

    def build_url(self) -> str:
        """Build the full connection URL, including the plaintext password.

        Returns
        -------
        str
            SQLAlchemy-compatible connection URL. For SQLite, returns
            ``sqlite:///<database>`` (or ``sqlite:///:memory:`` when
            ``database`` is not set).
        """
        return self._build_url(hide_password=False)

    def safe_url(self) -> str:
        """Build the connection URL with the password redacted.

        Safe for logging and display. Identical to ``build_url()`` for SQLite
        connections, which carry no password.

        Returns
        -------
        str
            Connection URL with ``***`` substituted for the password field.
        """
        return self._build_url(hide_password=True)


class ProviderConfig(BaseModel):
    """Concrete connection to one LLM provider: which one, and how to reach it.

    Peer of :class:`DatabaseConfig` for LLM/embedding backends instead of
    databases. Referenced by :attr:`ModelConfig.provider`. Each entry
    under ``[providers]`` in ``config.toml`` maps to one instance of this
    model.

    API keys are stored in plaintext for now, the same documented
    limitation :attr:`DatabaseConfig.password` already carries; secret
    management support is planned for a future release for both.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        description="Provider key, e.g. 'ollama', 'llamacpp', 'vllm', 'openai', 'anthropic', 'gemini'."
    )
    base_url: str | None = Field(
        default=None,
        description="Base URL for this specific deployment of the provider (a local llama-server, a cloud vendor endpoint, and so on).",
    )
    api_key: str | None = Field(
        default=None,
        description="Plaintext API key for this deployment, if one is required. Secret management support is planned for a future release.",
    )


class ModelConfig(BaseModel):
    """A named, reusable, concretely-configured model.

    Peer of :class:`ResourceConfig` for LLM/embedding backends instead of
    databases. The unit that consuming packages reference by name (e.g. a
    package's ``embedding_model_name`` field just names an entry here).
    Each entry under ``[models]`` in ``config.toml`` maps to one instance
    of this model.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        description="Name of the provider entry (from [providers]) this model is served through."
    )
    model: str = Field(
        description="Model name or identifier passed to the provider."
    )
    embedding_dim: int | None = Field(
        default=None,
        description="Embedding dimension override. Unset lets the provider's own discovery (fast path or live probe) determine it.",
    )
    document_prefix: str | None = Field(
        default=None,
        description="Prefix prepended to document/passage text before embedding, for asymmetric embedding models (e.g. nomic-embed-text, E5, BGE).",
    )
    query_prefix: str | None = Field(
        default=None,
        description="Prefix prepended to query text before embedding, for asymmetric embedding models (e.g. nomic-embed-text, E5, BGE).",
    )
    configuration: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form per-model knobs (max_tokens, temperature, and so on) with no dedicated field, passed through verbatim to the underlying call.",
    )


class ResourceConfig(BaseModel):
    """Maps the OMOP logical roles (CDM, vocab, results) to named databases and schema names.

    The unit that consuming packages configure once and reference by name.
    Most packages only need a single ``cdm_db`` resource. Each entry under
    ``[resources]`` in ``config.toml`` maps to one instance of this model.
    """

    model_config = ConfigDict(extra="forbid")

    database: str = Field(
        description="Name of the database entry (from [databases]) used as the primary CDM server."
    )
    vocab_database: str | None = Field(
        default=None,
        description="Name of the database entry for vocabulary tables. Falls back to database when not set.",
    )
    cdm_schema: str = Field(
        description="Schema where CDM clinical tables live."
    )
    vocab_schema: str | None = Field(
        default=None,
        description="Vocabulary schema. Falls back to cdm_schema when not set.",
    )
    results_schema: str | None = Field(
        default=None,
        description="Achilles / Atlas results schema.",
    )


class ToolConfig(BaseModel):
    """Per-package section in ``config.toml`` (``[tools.<name>]``).

    ``extra`` holds the package-specific typed fields declared on the
    package's :class:`~oa_configurator.PackageConfigBase` subclass.
    """

    model_config = ConfigDict(extra="forbid")

    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Package-specific key/value pairs. Each package defines its own typed fields that map here.",
    )


class ProfileOverrideConfig(BaseModel):
    """Named environment overlay (``[profiles.<name>]`` in ``config.toml``).

    Entries replace base entries with the same name when the profile is active.
    Anything not mentioned in the profile is inherited from the base config unchanged.
    Useful for switching between local-dev and production databases without
    editing the base config.
    """

    model_config = ConfigDict(extra="forbid")

    databases: dict[str, DatabaseConfig] = Field(
        default_factory=dict,
        description="Database configs that replace or extend the base databases.",
    )
    resources: dict[str, ResourceConfig] = Field(
        default_factory=dict,
        description="Resource configs that replace or extend the base resources.",
    )
    providers: dict[str, ProviderConfig] = Field(
        default_factory=dict,
        description="Provider configs that replace or extend the base providers.",
    )
    models: dict[str, ModelConfig] = Field(
        default_factory=dict,
        description="Model configs that replace or extend the base models.",
    )
    tools: dict[str, ToolConfig] = Field(
        default_factory=dict,
        description="Tool configs that replace or extend the base tool configs.",
    )


class StackConfig(BaseModel):
    """Root model for ``~/.config/omop/config.toml``.

    Holds the entire OMOP stack configuration in one object: named database
    connections, logical resources, per-package tool sections, and environment
    profiles. Loaded from disk by
    :func:`~oa_configurator.loader.load_stack_config`; constructed in memory
    via :meth:`for_session` for tests and scripts (no file I/O).
    """

    model_config = ConfigDict(extra="forbid")

    active_profile: str | None = Field(
        default=None,
        description="Name of the profile to activate. Can be overridden by the OA_ACTIVE_PROFILE env var.",
    )
    databases: dict[str, DatabaseConfig] = Field(
        default_factory=dict,
        description="Named database configurations (server address, credentials, target database).",
    )
    resources: dict[str, ResourceConfig] = Field(
        default_factory=dict,
        description="Named logical role bundles mapping CDM roles to databases and schemas.",
    )
    providers: dict[str, ProviderConfig] = Field(
        default_factory=dict,
        description="Named LLM/embedding provider connections.",
    )
    models: dict[str, ModelConfig] = Field(
        default_factory=dict,
        description="Named, concretely-configured models, each served through a provider.",
    )
    tools: dict[str, ToolConfig] = Field(
        default_factory=dict,
        description="Per-package configuration sections.",
    )
    profiles: dict[str, ProfileOverrideConfig] = Field(
        default_factory=dict,
        description="Named environment overlays.",
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="Logging configuration. Optional; defaults to WARNING level with no handler.",
    )
    _loaded_path: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_references(self) -> StackConfig:
        """Ensure all named cross-references point at configured objects."""
        for rname, resource in self.resources.items():
            self._check_resource_refs(resource, self.databases, f"resources.{rname}")
        for mname, model in self.models.items():
            self._check_model_refs(model, self.providers, f"models.{mname}")
        for pname, profile in self.profiles.items():
            effective_dbs = {**self.databases, **profile.databases}
            effective_providers = {**self.providers, **profile.providers}
            for rname, resource in profile.resources.items():
                self._check_resource_refs(resource, effective_dbs, f"profiles.{pname}.resources.{rname}")
            for mname, model in profile.models.items():
                self._check_model_refs(model, effective_providers, f"profiles.{pname}.models.{mname}")
        return self

    @staticmethod
    def _check_resource_refs(
        resource: ResourceConfig,
        databases: dict[str, DatabaseConfig],
        location: str,
    ) -> None:
        for field, name in (("database", resource.database), ("vocab_database", resource.vocab_database)):
            if name is not None and name not in databases:
                raise ValueError(
                    f"{location}.{field} references unknown database {name!r}"
                )

    @staticmethod
    def _check_model_refs(
        model: ModelConfig,
        providers: dict[str, ProviderConfig],
        location: str,
    ) -> None:
        if model.provider not in providers:
            raise ValueError(
                f"{location}.provider references unknown provider {model.provider!r}"
            )

    @classmethod
    def for_session(
        cls,
        *,
        databases: dict[str, DatabaseConfig] | None = None,
        resources: dict[str, ResourceConfig] | None = None,
        providers: dict[str, ProviderConfig] | None = None,
        models: dict[str, ModelConfig] | None = None,
        tools: dict[str, ToolConfig] | None = None,
        profiles: dict[str, ProfileOverrideConfig] | None = None,
        active_profile: str | None = None,
    ) -> StackConfig:
        """Build a config in memory without a TOML file.

        Intended for tests and scripts. Cross-references are validated at
        construction time, same as for file-loaded configs.

        Notes
        -----
        Pydantic still coerces a raw dict (e.g. one shaped like a parsed TOML
        table) into the corresponding model at validation time, so passing
        plain dicts keeps working at runtime. The parameter types above are
        the strict, intended shape; prefer constructing ResourceConfig,
        ToolConfig, and ProfileOverrideConfig instances directly so a renamed
        field is caught by the type checker instead of only at validation time.
        """
        return cls(
            active_profile=active_profile,
            databases=databases or {},
            resources=resources or {},
            providers=providers or {},
            models=models or {},
            tools=tools or {},
            profiles=profiles or {},
        )

    def bind_loaded_path(self, path: Path) -> None:
        """Record the path of the TOML file this config was loaded from."""
        self._loaded_path = path.expanduser().resolve()

    @property
    def loaded_path(self) -> Path | None:
        """Path of the TOML file this config was loaded from, if any."""
        return self._loaded_path

    def database_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured database names."""
        return tuple(sorted(self.databases))

    def resource_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured resource names."""
        return tuple(sorted(self.resources))

    def provider_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured provider names."""
        return tuple(sorted(self.providers))

    def model_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured model names."""
        return tuple(sorted(self.models))

    def tool_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured tool names."""
        return tuple(sorted(self.tools))

    def profile_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured profile names."""
        return tuple(sorted(self.profiles))


