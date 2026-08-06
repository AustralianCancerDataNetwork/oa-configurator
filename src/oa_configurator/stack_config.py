"""Root config model: StackConfig and the cross-domain RefTo machinery it validates.

The concrete per-domain schemas (ConnectionConfig/DatabaseConfig,
ProviderConfig/ModelConfig) live under :mod:`oa_configurator.domains`.
This module is the one place that needs to know about all of them at once,
to build _REF_SECTIONS and the root :class:`StackConfig`.

Only imports what it actually uses internally: domain schemas for field
types and _REF_SECTIONS, plus _iter_refs for the ref-walking helpers
below. Does not re-export them. :mod:`oa_configurator`, the top-level
package, is the one place that re-exports every public type, each
imported from the module that actually defines it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from .domains.llm.schema import ModelConfig, ProviderConfig
from .domains.resources.schema import CDMDatabaseConfig, ConnectionConfig, DatabaseEntry, GenericDatabaseConfig
from .domains.vector_stores.schema import VectorStoreConfig
from .logging_config import LoggingConfig
from .refs import _iter_refs


def unresolved_refs(instance: BaseModel, config: StackConfig) -> list[tuple[str, str, str]]:
    """Find every RefTo-marked field on instance whose value doesn't resolve
    against config.

    One pure walk shared by every caller that needs to check this: a
    StackConfig-level validator, a resolved package config, a freshly-built
    CLI entry before it's saved. Each wraps the same walk with its own error
    type instead of re-implementing it.

    Checks existence only. A value that exists but is the wrong concrete
    subtype, for example a RefTo(CDMDatabaseConfig) field pointing at a
    GenericDatabaseConfig entry, is not unresolved. See
    :func:`mismatched_kind_refs` for that, checked separately so the two
    failure modes get distinct, correctly actionable wording.

    Parameters
    ----------
    instance : BaseModel
        The object whose RefTo-marked fields are being checked.
    config : StackConfig
        The stack config to resolve field values against.

    Returns
    -------
    list[tuple[str, str, str]]
        One (field_name, value, section) triple per unresolved field.
        section is the StackConfig attribute the value should have been
        found in, e.g. "connections".
    """
    problems: list[tuple[str, str, str]] = []
    for field_name, ref in _iter_refs(type(instance)):
        value = getattr(instance, field_name)
        if value is None:
            continue
        section = _ref_section(ref.target, field_name=field_name)
        if value not in getattr(config, section):
            problems.append((field_name, value, section))
    return problems


def mismatched_kind_refs(
    instance: BaseModel, config: StackConfig
) -> list[tuple[str, str, type[BaseModel], type[BaseModel]]]:
    """Find every RefTo-marked field on instance whose value names an
    existing entry of the wrong concrete subtype.

    Parameters
    ----------
    instance : BaseModel
        The object whose RefTo-marked fields are being checked.
    config : StackConfig
        The stack config to resolve field values against.

    Returns
    -------
    list[tuple[str, str, type[BaseModel], type[BaseModel]]]
        One (field_name, value, expected_type, actual_type) tuple per
        mismatched field.
    """
    problems: list[tuple[str, str, type[BaseModel], type[BaseModel]]] = []
    for field_name, ref in _iter_refs(type(instance)):
        value = getattr(instance, field_name)
        if value is None or not isinstance(ref.target, type):
            continue
        section = _ref_section(ref.target, field_name=field_name)
        entry = getattr(config, section).get(value)
        if entry is not None and not isinstance(entry, ref.target):
            problems.append((field_name, value, ref.target, type(entry)))
    return problems

"""Which StackConfig dict a RefTo(target) marker resolves against,
and is allowed to reference. Deliberately exclude abstract base classes
like DatabaseConfig, as they are not meant to be constructed directly.
"""
_REF_SECTIONS: dict[type[BaseModel], str] = {
    ConnectionConfig: "connections",
    ProviderConfig: "providers",
    ModelConfig: "models",
    GenericDatabaseConfig: "databases",
    CDMDatabaseConfig: "databases",
    VectorStoreConfig: "vector_stores",
}


class UnknownRefTarget(TypeError):
    """A RefTo marker names a class that isn't registered in _REF_SECTIONS.

    Indicates a bug in the declaring package's own schema (e.g. `RefTo`
    against an abstract base like `DatabaseConfig`, or a class that was
    never meant to be a RefTo target at all), not a user configuration
    problem.
    """


def _ref_section(target: type[BaseModel], *, field_name: str | None = None) -> str:
    """Look up which StackConfig section a RefTo(target) resolves against.

    Raises :class:`UnknownRefTarget` with the field name (when known) and
    the list of valid targets, instead of letting a bare ``KeyError``
    surface a raw class repr.
    """
    try:
        return _REF_SECTIONS[target]
    except KeyError:
        valid = ", ".join(sorted(t.__name__ for t in _REF_SECTIONS))
        where = f"field {field_name!r} " if field_name else ""
        raise UnknownRefTarget(
            f"RefTo {where}targets {target.__name__!r}, which isn't a registered "
            f"RefTo section. Valid targets: {valid}."
        ) from None


class StackConfig(BaseModel):
    """Root model for ~/.config/omop/config.toml.

    Holds the entire OMOP stack configuration in one object: named
    connections, logical databases, and per-package tool sections. Loaded
    from disk by :func:`~oa_configurator.loader.load_stack_config`;
    constructed in memory via :meth:`for_session` for tests and scripts,
    with no file I/O.
    """

    model_config = ConfigDict(extra="forbid")

    connections: dict[str, ConnectionConfig] = Field(
        default_factory=dict,
        description="Named physical connections (server address, credentials, target database).",
    )
    databases: dict[str, DatabaseEntry] = Field(
        default_factory=dict,
        description="Named databases (generic or CDM/vocab/results bundles), keyed by kind.",
    )
    providers: dict[str, ProviderConfig] = Field(
        default_factory=dict,
        description="Named LLM/embedding provider connections.",
    )
    models: dict[str, ModelConfig] = Field(
        default_factory=dict,
        description="Named, concretely-configured models, each served through a provider.",
    )
    vector_stores: dict[str, VectorStoreConfig] = Field(
        default_factory=dict,
        description="Named vector-store backend configurations, referenced by embedding-capable packages.",
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
        for vname, vector_store in self.vector_stores.items():
            self._check_refs(vector_store, f"vector_stores.{vname}")
        return self

    def _check_refs(self, instance: BaseModel, location: str) -> None:
        for field_name, value, section in unresolved_refs(instance, self):
            raise ValueError(
                f"{location}.{field_name} references unknown {section[:-1]} {value!r}"
            )
        for field_name, value, expected, actual in mismatched_kind_refs(instance, self):
            raise ValueError(
                f"{location}.{field_name} requires a {expected.__name__} entry, but "
                f"{value!r} is a {actual.__name__}"
            )

    @classmethod
    def for_session(
        cls,
        *,
        connections: dict[str, ConnectionConfig] | None = None,
        databases: Mapping[str, DatabaseEntry] | None = None,
        providers: dict[str, ProviderConfig] | None = None,
        models: dict[str, ModelConfig] | None = None,
        vector_stores: dict[str, VectorStoreConfig] | None = None,
        tools: dict[str, dict[str, Any]] | None = None,
    ) -> StackConfig:
        """Build a config in memory without a TOML file.

        Intended for tests and scripts. Cross-references are validated at
        construction time, same as for file-loaded configs.

        Parameters
        ----------
        connections : dict[str, ConnectionConfig], optional
            Connection entries, keyed by name.
        databases : Mapping[str, DatabaseEntry], optional
            Database entries, keyed by name. ``Mapping``, not ``dict``, so a
            caller can pass just one concrete kind, for example
            ``dict[str, GenericDatabaseConfig]``, without a dict-invariance error.
        providers : dict[str, ProviderConfig], optional
            Provider entries, keyed by name.
        models : dict[str, ModelConfig], optional
            Model entries, keyed by name.
        vector_stores : dict[str, VectorStoreConfig], optional
            Vector-store entries, keyed by name.
        tools : dict[str, dict[str, Any]], optional
            Per-package ``[tools.<name>]`` sections, keyed by tool name.
        """
        return cls(
            connections=connections or {},
            databases=databases or {},
            providers=providers or {},
            models=models or {},
            vector_stores=vector_stores or {},
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

    def vector_store_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured vector store names."""
        return tuple(sorted(self.vector_stores))

    def tool_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured tool names."""
        return tuple(sorted(self.tools))
