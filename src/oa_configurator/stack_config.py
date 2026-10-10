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

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from .domains.llm.schema import ModelConfig, ProviderConfig
from .domains.resources.schema import (
    CDMDatabaseConfig,
    ConnectionConfig,
    DatabaseConfig,
    DatabaseEntry,
    GenericDatabaseConfig,
    _iter_schema_roles,
)
from .domains.resources.sql import (
    Role,
    is_ephemeral_url,
    supports_schemas,
    system_schemas,
)
from .domains.vector_stores.schema import VectorStoreConfig
from .logging_config import LoggingConfig
from .refs import SecretSafeBaseModel, _iter_refs

logger = logging.getLogger(__name__)


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

# Which StackConfig dict a RefTo(target) marker resolves against,
# and is allowed to reference. Deliberately exclude abstract base classes
# like DatabaseConfig, as they are not meant to be constructed directly.

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


class StackConfig(SecretSafeBaseModel):
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
            self._check_declared_schemas_supported(database, f"databases.{name}")
            self._check_declared_schema_names_valid(database, f"databases.{name}")
            self._check_connections_share_test_only(database, f"databases.{name}")
        for mname, model in self.models.items():
            self._check_refs(model, f"models.{mname}")
        for vname, vector_store in self.vector_stores.items():
            self._check_refs(vector_store, f"vector_stores.{vname}")
        self._check_connections_distinct()
        self._check_database_entries_exclusive()
        self._check_test_only_not_a_production_twin()
        self._check_no_schema_collision_on_one_connection()
        self._warn_vocab_connection_collisions()
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

    def _check_declared_schemas_supported(self, database: DatabaseConfig, location: str) -> None:
        """Checks each configured connection whether schemas are configured and supported
        by the dialect.

        Raises
        ------
        ValueError
            If a connection has no schema concept but schemas are configured for it.
        
        Notes
        -----
        Checks each distinct *connection* once to prevent duplicate checks
        on connections that are shared across multiple fields.

        Schema collision is checked at ``ResolvedDatabase.create_engine()`` time,
        since a reservation only matters once a connection actually exists to share it with.
        This check is connection-free and may run with no reachable database at alll
        """
        checked_connections: set[str] = set()
        for field_name, role in _iter_schema_roles(type(database)):
            value = getattr(database, field_name)
            if value is None:
                continue
            connection_name = database.connection_name_for_role(role)
            if connection_name in checked_connections:
                continue
            connection = self.connections.get(connection_name)
            if connection is not None and not supports_schemas(connection.dialect_name):
                raise ValueError(
                    f"{location}.{field_name}={value!r} is set, but connection "
                    f"{connection_name!r} ({connection.dialect}) has no schema concept."
                )
            checked_connections.add(connection_name)

    def _check_declared_schema_names_valid(self, database: DatabaseConfig, location: str) -> None:
        """Reject an obviously-wrong configured schema name: empty, all
        whitespace, leading/trailing whitespace, a dialect's own system
        schema (e.g. Postgres's ``pg_catalog``), or longer than Postgres's
        63-byte identifier limit.

        Raises
        ------
        ValueError
            If a configured schema name fails any of the above checks.
        """
        for field_name, role in _iter_schema_roles(type(database)):
            value = getattr(database, field_name)
            if value is None:
                continue
            if not value.strip() or value != value.strip():
                raise ValueError(
                    f"{location}.{field_name}={value!r} must not be empty or have "
                    "leading/trailing whitespace."
                )
            if len(value.encode("utf-8")) > 63:
                raise ValueError(
                    f"{location}.{field_name}={value!r} is longer than 63 bytes, "
                    "Postgres's own identifier limit."
                )
            connection_name = database.connection_name_for_role(role)
            connection = self.connections.get(connection_name)
            if connection is not None and value.lower() in system_schemas(connection.dialect_name):
                raise ValueError(
                    f"{location}.{field_name}={value!r} names a system schema reserved by "
                    f"{connection.dialect}."
                )

    def _check_connections_share_test_only(self, database: DatabaseConfig, location: str) -> None:
        """Every connection *database* references (primary and, for a CDM
        entry, vocab) must have the same ``test_only`` value.

        The schema-provenance guard now uses ``test_only`` as the switch
        between raising on drift and re-baselining (see
        ``guard_schema_provenance_for``), so a CDM entry with a test-marked
        primary and a production ``vocab_connection`` (or vice versa) would
        silently apply the wrong policy to one of its own roles.
        """
        values: dict[bool, str] = {}
        for _, role in _iter_schema_roles(type(database)):
            connection_name = database.connection_name_for_role(role)
            connection = self.connections.get(connection_name)
            if connection is None:
                continue
            values.setdefault(connection.test_only, connection_name)
        if len(values) > 1:
            raise ValueError(
                f"{location}: its connections disagree on test_only ({values}). Every "
                "connection referenced by one database entry must share test_only."
            )

    def _identity_for_connection_name(self, connection_name: str) -> tuple[Any, ...] | None:
        """*connection_name*'s physical identity, or None if unresolvable or
        genuinely ephemeral."""
        connection = self.connections.get(connection_name)
        if connection is None or is_ephemeral_url(connection.safe_url()):
            return None
        return connection.physical_identity()

    def _check_connections_distinct(self) -> None:
        """No two production ``[connections.*]`` entries may share a
        physical identity.

        Exceptions:
        - ``test_only`` connections: tests routinely clone the available
            test server under several connection names purely to exercise
            multi-connection routing logic
        - ephemeral SQLite connections: every ``:memory:`` URL is a distinct
            database.
        """
        seen: dict[tuple[Any, ...], str] = {}
        for name, connection in self.connections.items():
            if connection.test_only or is_ephemeral_url(connection.safe_url()):
                continue
            identity = connection.physical_identity()
            if identity in seen:
                raise ValueError(
                    f"connections.{name} describes the same connection as "
                    f"connections.{seen[identity]!r} (same dialect, host, port, database, "
                    "and user). Give it different credentials, or remove the duplicate."
                )
            seen[identity] = name

    def _check_database_entries_exclusive(self) -> None:
        """No two CDM database entries, and no two vector-store entries, may
        share a primary-connection physical identity.

        Notes
        -----
        No cross-check between CDM and vector-store entries: a vector store
        sharing a connection with a CDM entry is the "extend this CDM's database
        with embedding storage" pattern
        """
        seen_cdm: dict[tuple[Any, ...], str] = {}
        for name, database in self.databases.items():
            if not isinstance(database, CDMDatabaseConfig):
                continue
            identity = self._identity_for_connection_name(database.connection_name_for_role(Role.PRIMARY))
            if identity is None:
                continue
            if identity in seen_cdm:
                raise ValueError(
                    f"databases.{name} and databases.{seen_cdm[identity]!r} are both CDM "
                    "entries on the same physical connection. Each CDM database needs its own "
                    "connection (vocab_connection may still be shared across CDM entries)."
                )
            seen_cdm[identity] = name

        seen_vector_store: dict[tuple[Any, ...], str] = {}
        for name, vector_store in self.vector_stores.items():
            backing = self.databases.get(vector_store.database)
            if backing is None:
                continue
            identity = self._identity_for_connection_name(backing.connection_name_for_role(Role.PRIMARY))
            if identity is None:
                continue
            if identity in seen_vector_store:
                raise ValueError(
                    f"vector_stores.{name} and vector_stores.{seen_vector_store[identity]!r} "
                    "are both backed by the same physical connection. Each vector store needs "
                    "its own database."
                )
            seen_vector_store[identity] = name

    def _check_test_only_not_a_production_twin(self) -> None:
        """No ``test_only`` connection may share a physical identity with a
        non-``test_only`` connection.

        ``_check_connections_distinct`` deliberately exempts every
        ``test_only`` connection from its own check (tests routinely clone the
        available test server under several names), which leaves a
        ``test_only`` connection free to duplicate a *production* connection's
        identity. That matters here specifically because ``test_only`` is the
        re-baseline switch for the schema-provenance guard: a twin would
        silently re-baseline production rows instead of raising on drift.

        ``user`` is excluded from the comparison (a ``test_only`` connection
        legitimately uses different credentials on the same server), matching
        ``_check_connections_distinct``'s own exemption rationale.
        """
        production_identities: dict[tuple[Any, ...], str] = {}
        for name, connection in self.connections.items():
            if connection.test_only or is_ephemeral_url(connection.safe_url()):
                continue
            identity = connection.physical_identity()[:-1]
            production_identities.setdefault(identity, name)

        for name, connection in self.connections.items():
            if not connection.test_only or is_ephemeral_url(connection.safe_url()):
                continue
            identity = connection.physical_identity()[:-1]
            other = production_identities.get(identity)
            if other is not None:
                raise ValueError(
                    f"connections.{name} is test_only but describes the same physical "
                    f"database as connections.{other!r} (same dialect, host, port, and "
                    "database), which is not test_only. A test_only connection must never "
                    "be able to re-baseline a production database's schema provenance."
                )

    def _check_no_schema_collision_on_one_connection(self) -> None:
        """No two database entries sharing one connection may declare the
        same physical schema name.

        Schema collision is otherwise only caught at
        ``ResolvedDatabase.create_engine()`` time, and only once the shared
        schema is non-empty (the first entry to create tables there "wins";
        the second looks adopted rather than colliding). This is the static,
        connection-free half of that check.
        """
        seen: dict[tuple[str, str], tuple[str, str]] = {}
        for name, database in self.databases.items():
            for field_name, role in _iter_schema_roles(type(database)):
                value = getattr(database, field_name)
                if value is None:
                    continue
                connection_name = database.connection_name_for_role(role)
                key = (connection_name, value)
                other = seen.get(key)
                if other is not None and other[0] != name:
                    raise ValueError(
                        f"databases.{name}.{field_name}={value!r} and "
                        f"databases.{other[0]}.{other[1]}={value!r} declare the same "
                        f"physical schema on connection {connection_name!r}. Each database "
                        "entry needs its own schema on a shared connection."
                    )
                seen.setdefault(key, (name, field_name))

    def _warn_vocab_connection_collisions(self) -> None:
        """Warn when a CDM's ``vocab_connection`` physically
        coincides with another entry's primary connection.
        """
        primaries: dict[tuple[Any, ...], str] = {}
        for name, database in self.databases.items():
            identity = self._identity_for_connection_name(database.connection_name_for_role(Role.PRIMARY))
            if identity is not None:
                primaries.setdefault(identity, name)
        for name, vector_store in self.vector_stores.items():
            backing = self.databases.get(vector_store.database)
            if backing is None:
                continue
            identity = self._identity_for_connection_name(backing.connection_name_for_role(Role.PRIMARY))
            if identity is not None:
                primaries.setdefault(identity, name)

        for name, database in self.databases.items():
            if not isinstance(database, CDMDatabaseConfig) or database.vocab_connection is None:
                continue
            vocab_identity = self._identity_for_connection_name(database.vocab_connection)
            if vocab_identity is None:
                continue
            own_identity = self._identity_for_connection_name(database.connection_name_for_role(Role.PRIMARY))
            if vocab_identity == own_identity:
                continue
            other = primaries.get(vocab_identity)
            if other is not None and other != name:
                logger.warning(
                    "databases.%s.vocab_connection physically coincides with %s's primary "
                    "connection. This is allowed and drift-safe, but double-check it's "
                    "intentional and not a copy-paste/typo.",
                    name, other,
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
