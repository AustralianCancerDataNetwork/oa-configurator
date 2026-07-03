"""Typed Pydantic models for the OMOP stack configuration."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from sqlalchemy.engine import URL, Engine

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

    name: str = Field(
        default="",
        exclude=True,
        repr=False,
        description="Logical name of this connection as declared in [databases]. Set by the resolver; not read from TOML.",
    )
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

    @model_validator(mode="after")
    def _name_is_reserved(self) -> DatabaseConfig:
        if "name" in self.model_fields_set:
            raise ValueError(
                "DatabaseConfig.name is reserved for the resolver and must not be set in "
                "config.toml. The logical name comes from the key under [databases] "
                "(e.g. [databases.cdm] makes name='cdm' available after resolution)."
            )
        return self

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
        if self.dialect.startswith("sqlite") and "read_only" in kwargs:
            kwargs.pop("read_only", None)
        return sa.create_engine(self.build_url(), **kwargs)


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


class KnowledgeKind(StrEnum):
    local_path = "local_path"


class KnowledgeResourceConfig(BaseModel):
    """Shared interface anchor for all knowledge resource config types.

    Enforces the ``kind`` field on every subclass and provides the base type
    used for resolver internals, ``isinstance`` dispatch, and method parameters.
    Pydantic field declarations use :data:`KnowledgeResourceType` (not this
    class directly) so that deserialization dispatches to the correct concrete
    subclass based on ``kind``.

    To add a new kind: add a member to :class:`KnowledgeKind`, write a subclass
    with ``kind: Literal[KnowledgeKind.<member>]``, and extend
    :data:`KnowledgeResourceType` with the new type.
    """

    model_config = ConfigDict(extra="forbid")
    kind: KnowledgeKind


class LocalPathKnowledgeResource(KnowledgeResourceConfig):
    """Knowledge source backed by a local filesystem directory.

    ``root`` may be an absolute path or a path relative to the config file.
    The resolver always expands it to absolute before returning a resolved handle.
    """

    kind: Literal[KnowledgeKind.local_path] = KnowledgeKind.local_path  # type: ignore[assignment]
    root: Path = Field(description="Root directory for this knowledge source.")


# Discriminated union for Pydantic field declarations. Pydantic reads ``kind``
# from the raw TOML dict and dispatches to the matching concrete class.
# This is the single place to extend when a new KnowledgeResource kind is added:
#   KnowledgeResourceType = Annotated[LocalPathKnowledgeResource | NewKind, Field(discriminator="kind")]
KnowledgeResourceType = Annotated[
    LocalPathKnowledgeResource,
    Field(discriminator="kind"),
]

# Covariant parameter type for methods that accept any knowledge resource mapping.
# Mapping so callers can pass dict[str, <ConcreteSubclass>] without
# dict-invariance type errors; we only read from these mappings, never write.
KnowledgeResourceMap = Mapping[str, KnowledgeResourceConfig]


class ToolConfig(BaseModel):
    """Per-package section in ``config.toml`` (``[tools.<name>]``).

    ``default_resource`` names which resource this package reads from.
    ``default_knowledge_resource`` names which shared knowledge source this
    package reads from.
    ``extra`` holds the package-specific typed fields declared on the
    package's :class:`~oa_configurator.PackageConfigBase` subclass.
    """

    model_config = ConfigDict(extra="forbid")

    default_resource: str | None = Field(
        default=None,
        description="Resource name this tool uses when none is specified by the caller.",
    )
    default_knowledge_resource: str | None = Field(
        default=None,
        description="Knowledge resource name this tool uses when none is specified by the caller.",
    )
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
    knowledge_resources: dict[str, KnowledgeResourceType] = Field(
        default_factory=dict,
        description="Knowledge resource configs that replace or extend the base knowledge resources.",
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
    knowledge_resources: dict[str, KnowledgeResourceType] = Field(
        default_factory=dict,
        description="Named shared knowledge source configurations.",
    )
    tools: dict[str, ToolConfig] = Field(
        default_factory=dict,
        description="Per-package configuration sections.",
    )
    profiles: dict[str, ProfileOverrideConfig] = Field(
        default_factory=dict,
        description="Named environment overlays.",
    )
    resource_aliases: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Maps semantic resource names to user-chosen resource names. "
            "Example: cdm_db = 'my_production_cdm'; all packages that look "
            "for 'cdm_db' automatically resolve to 'my_production_cdm'. "
            "Alias targets must exist at the base config level, not only inside a profile."
        ),
    )
    knowledge_resource_aliases: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Maps semantic knowledge resource names to user-chosen resource names. "
            "Alias targets must exist at the base config level, not only inside a profile."
        ),
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
        for tname, tool in self.tools.items():
            self._check_tool_refs(
                tool,
                self.resources,
                self.resource_aliases,
                self.knowledge_resources,
                self.knowledge_resource_aliases,
                f"tools.{tname}",
            )
        for pname, profile in self.profiles.items():
            effective_dbs = {**self.databases, **profile.databases}
            effective_res = {**self.resources, **profile.resources}
            effective_knowledge = {**self.knowledge_resources, **profile.knowledge_resources}
            for rname, resource in profile.resources.items():
                self._check_resource_refs(resource, effective_dbs, f"profiles.{pname}.resources.{rname}")
            for tname, tool in profile.tools.items():
                self._check_tool_refs(
                    tool,
                    effective_res,
                    self.resource_aliases,
                    effective_knowledge,
                    self.knowledge_resource_aliases,
                    f"profiles.{pname}.tools.{tname}",
                )
        for alias_key, alias_target in self.resource_aliases.items():
            if alias_target not in self.resources:
                raise ValueError(
                    f"resource_aliases.{alias_key!r} references unknown resource {alias_target!r}. "
                    "Note: alias targets must exist at the base config level, not only inside a profile."
                )
        for alias_key, alias_target in self.knowledge_resource_aliases.items():
            if alias_target not in self.knowledge_resources:
                raise ValueError(
                    f"knowledge_resource_aliases.{alias_key!r} references unknown knowledge resource "
                    f"{alias_target!r}. Note: alias targets must exist at the base config level, "
                    "not only inside a profile."
                )
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
    def _check_tool_refs(
        tool: ToolConfig,
        resources: dict[str, ResourceConfig],
        resource_aliases: dict[str, str],
        knowledge_resources: KnowledgeResourceMap,
        knowledge_resource_aliases: dict[str, str],
        location: str,
    ) -> None:
        if tool.default_resource is not None:
            effective = resource_aliases.get(tool.default_resource, tool.default_resource)
            if effective not in resources:
                raise ValueError(
                    f"{location}.default_resource references unknown resource {tool.default_resource!r}"
                )
        if tool.default_knowledge_resource is not None:
            effective = knowledge_resource_aliases.get(
                tool.default_knowledge_resource,
                tool.default_knowledge_resource,
            )
            if effective not in knowledge_resources:
                raise ValueError(
                    f"{location}.default_knowledge_resource references unknown knowledge resource "
                    f"{tool.default_knowledge_resource!r}"
                )

    @classmethod
    def for_session(
        cls,
        *,
        databases: dict[str, DatabaseConfig] | None = None,
        resources: dict[str, ResourceConfig] | None = None,
        knowledge_resources: KnowledgeResourceMap | None = None,
        tools: dict[str, ToolConfig] | None = None,
        profiles: dict[str, ProfileOverrideConfig] | None = None,
        active_profile: str | None = None,
        resource_aliases: dict[str, str] | None = None,
        knowledge_resource_aliases: dict[str, str] | None = None,
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
            knowledge_resources=knowledge_resources or {},  # type: ignore[arg-type]
            tools=tools or {},
            profiles=profiles or {},
            resource_aliases=resource_aliases or {},
            knowledge_resource_aliases=knowledge_resource_aliases or {},
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

    def knowledge_resource_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured knowledge resource names."""
        return tuple(sorted(self.knowledge_resources))

    def tool_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured tool names."""
        return tuple(sorted(self.tools))

    def profile_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured profile names."""
        return tuple(sorted(self.profiles))
