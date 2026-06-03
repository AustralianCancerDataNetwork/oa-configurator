"""Typed Pydantic models for the OMOP stack configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from sqlalchemy.engine import URL

from .logging_config import LoggingConfig


class ConnectionConfig(BaseModel):
    """Named database connection endpoint.

    Stores all parameters needed to build a SQLAlchemy connection URL.
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
    database: str | None = Field(
        default=None,
        description="Database name. For SQLite use ':memory:' or an absolute path.",
    )
    read_only: bool = Field(
        default=False,
        description="Hint only; enforcement depends on the dialect.",
    )

    def _build_url(self, hide_password: bool) -> str:
        if self.dialect.startswith("sqlite"):
            db = self.database or ":memory:"
            return f"sqlite:///{db}"
        return URL.create(
            drivername=self.dialect,
            username=self.user,
            password=self.password,
            host=self.host or "localhost",
            port=self.port,
            database=self.database or "",
        ).render_as_string(hide_password=hide_password)

    def build_url(self) -> str:
        """Full connection URL including plaintext password."""
        return self._build_url(hide_password=False)

    def safe_url(self) -> str:
        """Connection URL with password redacted; safe for logs and display."""
        return self._build_url(hide_password=True)


class ResourceConfig(BaseModel):
    """Maps logical OMOP CDM roles to named connections and schema names.

    A resource is the unit of database targeting that consuming packages
    interact with. Most packages only need the ``default`` resource.
    """

    model_config = ConfigDict(extra="forbid")

    primary_db: str = Field(
        description="Name of the connection used for the CDM server."
    )
    vocab_db: str | None = Field(
        default=None,
        description="Name of the connection for vocabulary tables. Falls back to primary_db when not set.",
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
    """Per-package configuration section.

    Package-specific fields are declared on a ``PackageConfigBase`` subclass
    and stored in ``extra``. The core model only knows about
    ``default_resource``.
    """

    model_config = ConfigDict(extra="forbid")

    default_resource: str | None = Field(
        default=None,
        description="Resource name this tool uses when none is specified by the caller.",
    )
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Package-specific key/value pairs. Each package defines its own typed fields that map here.",
    )


class ProfileOverrideConfig(BaseModel):
    """Profile overlay: connections, resources, and tools that replace base entries when the profile is active.

    Entries in a profile completely replace the base entry with the same name.
    Entries not mentioned in the profile are inherited from the base config unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    connections: dict[str, ConnectionConfig] = Field(
        default_factory=dict,
        description="Connection configs that replace or extend the base connections.",
    )
    resources: dict[str, ResourceConfig] = Field(
        default_factory=dict,
        description="Resource configs that replace or extend the base resources.",
    )
    tools: dict[str, ToolConfig] = Field(
        default_factory=dict,
        description="Tool configs that replace or extend the base tool configs.",
    )


class StackConfig(BaseModel):
    """Root configuration for the OMOP stack.

    Loaded from ``~/.config/omop/config.toml`` by :func:`~oa_configurator.loader.load_stack_config`.
    Can also be constructed in memory via :meth:`for_session` (no file I/O).
    """

    model_config = ConfigDict(extra="forbid")

    active_profile: str | None = Field(
        default=None,
        description="Name of the profile to activate. Can be overridden by the OA_ACTIVE_PROFILE env var.",
    )
    connections: dict[str, ConnectionConfig] = Field(
        default_factory=dict,
        description="Named database connection endpoints.",
    )
    resources: dict[str, ResourceConfig] = Field(
        default_factory=dict,
        description="Named logical role bundles mapping CDM roles to connections and schemas.",
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
            "Example: cdm_db = 'my_production_cdm' — all packages that look "
            "for 'cdm_db' automatically resolve to 'my_production_cdm'. "
            "Note: alias targets must exist at the base config level, not only inside a profile."
        ),
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="Logging configuration. Optional; defaults to WARNING level with no handler.",
    )
    _loaded_path: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_references(self) -> "StackConfig":
        """Ensure all named cross-references point at configured objects."""
        for rname, resource in self.resources.items():
            self._check_resource_refs(resource, self.connections, f"resources.{rname}")
        for tname, tool in self.tools.items():
            self._check_tool_refs(tool, self.resources, f"tools.{tname}")
        for pname, profile in self.profiles.items():
            effective_conns = {**self.connections, **profile.connections}
            effective_res = {**self.resources, **profile.resources}
            for rname, resource in profile.resources.items():
                self._check_resource_refs(resource, effective_conns, f"profiles.{pname}.resources.{rname}")
            for tname, tool in profile.tools.items():
                self._check_tool_refs(tool, effective_res, f"profiles.{pname}.tools.{tname}")
        for alias_key, alias_target in self.resource_aliases.items():
            if alias_target not in self.resources:
                raise ValueError(
                    f"resource_aliases.{alias_key!r} references unknown resource {alias_target!r}. "
                    "Note: alias targets must exist at the base config level, not only inside a profile."
                )
        return self

    @staticmethod
    def _check_resource_refs(
        resource: "ResourceConfig",
        connections: "dict[str, ConnectionConfig]",
        location: str,
    ) -> None:
        for field, name in (("primary_db", resource.primary_db), ("vocab_db", resource.vocab_db)):
            if name is not None and name not in connections:
                raise ValueError(
                    f"{location}.{field} references unknown connection {name!r}"
                )

    @staticmethod
    def _check_tool_refs(
        tool: "ToolConfig",
        resources: "dict[str, ResourceConfig]",
        location: str,
    ) -> None:
        if tool.default_resource is not None and tool.default_resource not in resources:
            raise ValueError(
                f"{location}.default_resource references unknown resource {tool.default_resource!r}"
            )

    @classmethod
    def for_session(
        cls,
        *,
        connections: dict | None = None,
        resources: dict | None = None,
        tools: dict | None = None,
        profiles: dict | None = None,
        active_profile: str | None = None,
        resource_aliases: dict[str, str] | None = None,
    ) -> "StackConfig":
        """Build a config in memory without a TOML file.

        Intended for tests and scripts. Cross-references are validated at
        construction time, same as for file-loaded configs.
        """
        return cls(
            active_profile=active_profile,
            connections=connections or {},
            resources=resources or {},
            tools=tools or {},
            profiles=profiles or {},
            resource_aliases=resource_aliases or {},
        )

    def bind_loaded_path(self, path: Path) -> None:
        """Record the path of the TOML file this config was loaded from."""
        self._loaded_path = path.expanduser().resolve()

    @property
    def loaded_path(self) -> Path | None:
        """Path of the TOML file this config was loaded from, if any."""
        return self._loaded_path

    def connection_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.connections))

    def resource_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.resources))

    def tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.tools))

    def profile_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.profiles))


