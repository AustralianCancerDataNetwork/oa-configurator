"""Root config model: StackConfig and the cross-domain RefTo machinery it validates.

The concrete per-domain schemas (ConnectionConfig/DatabaseConfig,
ProviderConfig/ModelConfig) live under :mod:`oa_configurator.domains`.
This module is the one place that needs to know about all of them at once,
to build ``_REF_SECTIONS`` and the root :class:`StackConfig`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from .domains.llm.schema import ModelConfig, ProviderConfig
from .domains.resources.schema import ConnectionConfig, DatabaseConfig
from .logging_config import LoggingConfig
from .refs import RefTo, Sensitive, _iter_refs, is_sensitive

__all__ = [
    "ConnectionConfig",
    "DatabaseConfig",
    "ModelConfig",
    "ProviderConfig",
    "RefTo",
    "Sensitive",
    "StackConfig",
    "is_sensitive",
    "unresolved_refs",
]


def unresolved_refs(instance: BaseModel, config: StackConfig) -> list[tuple[str, str, str]]:
    """Return every RefTo-marked field on *instance* whose value doesn't
    resolve against *config*, as ``(field_name, value, section)`` triples
    (*section* is the StackConfig attribute the value should have been
    found in, e.g. ``"connections"``).

    One pure walk shared by every caller that needs to check this (a
    StackConfig-level validator, a resolved package config, a freshly-built
    CLI entry before it's saved). Each wraps the same walk with its own
    error type instead of re-implementing it.
    """
    problems: list[tuple[str, str, str]] = []
    for field_name, ref in _iter_refs(type(instance)):
        value = getattr(instance, field_name)
        if value is None:
            continue
        section = _REF_SECTIONS[ref.target]
        if value not in getattr(config, section):
            problems.append((field_name, value, section))
    return problems


_REF_SECTIONS: dict[type[BaseModel], str] = {
    ConnectionConfig: "connections",
    ProviderConfig: "providers",
    ModelConfig: "models",
    DatabaseConfig: "databases",
}
"""Which StackConfig dict a RefTo(target) marker resolves against."""


class StackConfig(BaseModel):
    """Root model for ``~/.config/omop/config.toml``.

    Holds the entire OMOP stack configuration in one object: named
    connections, logical databases, and per-package tool sections. Loaded
    from disk by :func:`~oa_configurator.loader.load_stack_config`;
    constructed in memory via :meth:`for_session` for tests and scripts (no
    file I/O).
    """

    model_config = ConfigDict(extra="forbid")

    connections: dict[str, ConnectionConfig] = Field(
        default_factory=dict,
        description="Named physical connections (server address, credentials, target database).",
    )
    databases: dict[str, DatabaseConfig] = Field(
        default_factory=dict,
        description="Named logical role bundles mapping CDM roles to connections and schemas.",
    )
    providers: dict[str, ProviderConfig] = Field(
        default_factory=dict,
        description="Named LLM/embedding provider connections.",
    )
    models: dict[str, ModelConfig] = Field(
        default_factory=dict,
        description="Named, concretely-configured models, each served through a provider.",
    )
    tools: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-package [tools.<name>] sections, keyed by tool_name.",
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="Logging configuration. Optional; defaults to WARNING level with no handler.",
    )
    _loaded_path: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_references(self) -> StackConfig:
        """Ensure every RefTo-marked field points at a configured entry."""
        for name, database in self.databases.items():
            self._check_refs(database, f"databases.{name}")
        for mname, model in self.models.items():
            self._check_refs(model, f"models.{mname}")
        return self

    def _check_refs(self, instance: BaseModel, location: str) -> None:
        for field_name, value, section in unresolved_refs(instance, self):
            raise ValueError(
                f"{location}.{field_name} references unknown {section[:-1]} {value!r}"
            )

    @classmethod
    def for_session(
        cls,
        *,
        connections: dict[str, ConnectionConfig] | None = None,
        databases: dict[str, DatabaseConfig] | None = None,
        providers: dict[str, ProviderConfig] | None = None,
        models: dict[str, ModelConfig] | None = None,
        tools: dict[str, dict[str, Any]] | None = None,
    ) -> StackConfig:
        """Build a config in memory without a TOML file.

        Intended for tests and scripts. Cross-references are validated at
        construction time, same as for file-loaded configs.

        Notes
        -----
        Pydantic still coerces a raw dict (e.g. one shaped like a parsed TOML
        table) into the corresponding model at validation time, so passing
        plain dicts keeps working at runtime. The parameter types above are
        the strict, intended shape; prefer constructing DatabaseConfig
        instances directly so a renamed field is caught by the type checker
        instead of only at validation time. ``tools`` stays a plain dict:
        it's the one section oa-configurator never types itself, since each
        package's own schema is only known lazily, via its
        ``PackageConfigBase`` subclass.
        """
        return cls(
            connections=connections or {},
            databases=databases or {},
            providers=providers or {},
            models=models or {},
            tools=tools or {},
        )

    def bind_loaded_path(self, path: Path) -> None:
        """Record the path of the TOML file this config was loaded from."""
        self._loaded_path = path.expanduser().resolve()

    @property
    def loaded_path(self) -> Path | None:
        """Path of the TOML file this config was loaded from, if any."""
        return self._loaded_path

    def connection_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured connection names."""
        return tuple(sorted(self.connections))

    def database_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured database names."""
        return tuple(sorted(self.databases))

    def provider_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured provider names."""
        return tuple(sorted(self.providers))

    def model_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured model names."""
        return tuple(sorted(self.models))

    def tool_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured tool names."""
        return tuple(sorted(self.tools))
