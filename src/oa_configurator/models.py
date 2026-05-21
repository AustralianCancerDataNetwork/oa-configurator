"""Typed models for the OMOP stack configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from sqlalchemy.engine import URL

from .logging_config import LoggingConfig


class ConnectionConfig(BaseModel):
    """One named database connection."""

    model_config = ConfigDict(extra="forbid")

    dialect: str                    # e.g. "postgresql+psycopg2", "sqlite"; no default
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None     # plaintext; secret management deferred to future iteration
    database: str | None = None
    read_only: bool = False

    def build_url(self) -> str:
        """Full URL including plaintext password."""
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
        ).render_as_string(hide_password=False)

    def safe_url(self) -> str:
        """URL with password redacted; safe for logs and display."""
        if self.dialect.startswith("sqlite"):
            return self.build_url()
        return URL.create(
            drivername=self.dialect,
            username=self.user,
            password=self.password,
            host=self.host or "localhost",
            port=self.port,
            database=self.database or "",
        ).render_as_string(hide_password=True)


class ResourceConfig(BaseModel):
    """Maps logical CDM roles to named connections and schema names."""

    model_config = ConfigDict(extra="forbid")

    primary_db: str                     # connection name for the CDM server
    vocab_db: str | None = None         # separate connection for vocabulary; falls back to primary_db
    cdm_schema: str                     # schema where CDM clinical tables live
    vocab_schema: str | None = None     # vocabulary schema; falls back to cdm_schema when None
    results_schema: str | None = None   # Achilles / Atlas results schema


class ToolConfig(BaseModel):
    """Per-tool section. Package-specific fields live in extra."""

    model_config = ConfigDict(extra="forbid")

    default_resource: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ProfileOverrideConfig(BaseModel):
    """Profile overlay: connections / resources / tools that replace base entries."""

    model_config = ConfigDict(extra="forbid")

    connections: dict[str, ConnectionConfig] = Field(default_factory=dict)
    resources: dict[str, ResourceConfig] = Field(default_factory=dict)
    tools: dict[str, ToolConfig] = Field(default_factory=dict)


class StackConfig(BaseModel):
    """Root configuration for the OMOP stack."""

    model_config = ConfigDict(extra="forbid")

    active_profile: str | None = None
    connections: dict[str, ConnectionConfig] = Field(default_factory=dict)
    resources: dict[str, ResourceConfig] = Field(default_factory=dict)
    tools: dict[str, ToolConfig] = Field(default_factory=dict)
    profiles: dict[str, ProfileOverrideConfig] = Field(default_factory=dict)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    _loaded_path: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_references(self) -> "StackConfig":
        """Ensure all named cross-references point at configured objects."""
        for rname, resource in self.resources.items():
            _check_resource_refs(resource, self.connections, f"resources.{rname}")
        for tname, tool in self.tools.items():
            _check_tool_refs(tool, self.resources, f"tools.{tname}")
        for pname, profile in self.profiles.items():
            effective_conns = {**self.connections, **profile.connections}
            effective_res = {**self.resources, **profile.resources}
            for rname, resource in profile.resources.items():
                _check_resource_refs(resource, effective_conns, f"profiles.{pname}.resources.{rname}")
            for tname, tool in profile.tools.items():
                _check_tool_refs(tool, effective_res, f"profiles.{pname}.tools.{tname}")
        return self

    @classmethod
    def for_session(
        cls,
        *,
        connections: dict | None = None,
        resources: dict | None = None,
        tools: dict | None = None,
        profiles: dict | None = None,
        active_profile: str | None = None,
    ) -> "StackConfig":
        """Build in memory without a TOML file. Intended for tests and scripts."""
        return cls(
            active_profile=active_profile,
            connections=connections or {},
            resources=resources or {},
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

    def connection_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.connections))

    def resource_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.resources))

    def tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.tools))

    def profile_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.profiles))


def _check_resource_refs(
    resource: ResourceConfig,
    connections: dict[str, ConnectionConfig],
    location: str,
) -> None:
    for field, name in (("primary_db", resource.primary_db), ("vocab_db", resource.vocab_db)):
        if name is not None and name not in connections:
            raise ValueError(
                f"{location}.{field} references unknown connection {name!r}"
            )


def _check_tool_refs(
    tool: ToolConfig,
    resources: dict[str, ResourceConfig],
    location: str,
) -> None:
    if tool.default_resource is not None and tool.default_resource not in resources:
        raise ValueError(
            f"{location}.default_resource references unknown resource {tool.default_resource!r}"
        )
