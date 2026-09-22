"""Resources domain: physical connections and logical CDM/vocab/results database bundles."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, Literal

from collections.abc import Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.engine import URL, Engine
import sqlalchemy as sa

from ...refs import RefTo, Secret, SecretSafeBaseModel
from .sql import (
    SCHEMA_TRANSLATE_MAP_KEY,
    Role,
    reject_reserved_schema,
    requires_host,
    schema_if_supported,
    supports_schemas,
)

if TYPE_CHECKING:
    from ...stack_config import StackConfig


class ConnectionConfig(SecretSafeBaseModel):
    """Complete specification of one physical database connection: server
    address, credentials, and target database.

    Referenced by :attr:`DatabaseConfig.connection` and
    :attr:`DatabaseConfig.vocab_connection`. Each entry under
    ``[connections]`` in ``config.toml`` maps to one instance of this model.

    Passwords are stored in plaintext for now; secret management support
    is planned for a future release.
    """

    model_config = ConfigDict(extra="forbid")

    dialect: str = Field(
        description="SQLAlchemy dialect string, e.g. 'postgresql+psycopg', 'sqlite'."
    )
    host: str | None = Field(
        default=None,
        description=(
            "Hostname or IP address. Required for every dialect except SQLite, which "
            "connects to a local file and has no host to speak of."
        ),
    )
    port: int | None = Field(default=None, description="Port number.")
    user: str | None = Field(default=None, description="Database username.")
    password: Secret = Field(
        default=None,
        description="Plaintext password. Secret management support is planned for a future release.",
    )
    database_name: str | None = Field(
        default=None,
        description=(
            "Database name on the server. Required for SQLite, which has no implicit "
            "default: pass an absolute path, or ':memory:' for an in-memory database."
        ),
    )
    test_only: bool = Field(
        default=False,
        description=(
            "Marks this connection as intended for testing only. "
            "It will be excluded from production database prompts and "
            "used as a safety check to prevent accidental test operations "
            "on production data."
        ),
    )

    @model_validator(mode="after")
    def _check_required_fields(self) -> ConnectionConfig:
        """Enforce host/database_name requiredness at construction time.

        Routed through the same DialectProfile registry every other
        dialect-varying decision in this codebase uses, rather than a
        hand-written binary check. A dialect not in the registry raises
        via _profile_for() rather than being silently misclassified.
        Mirrors _build_url_obj()'s own checks, which stay in place as
        defense-in-depth: neither model_copy(update=...) nor direct
        attribute mutation re-runs this validator on a non-frozen model
        with no validate_assignment.
        """
        if requires_host(self.dialect_name):
            if not self.host:
                raise ValueError(
                    "ConnectionConfig has no `host` set and no longer defaults to 'localhost'."
                    " Set `host` explicitly in config.toml."
                )
        elif not self.database_name:
            raise ValueError(
                f"ConnectionConfig has no `database_name` set for dialect {self.dialect_name!r}"
                " (a file-based dialect) and no longer defaults to ':memory:'. Set"
                " `database_name` explicitly, passing ':memory:' if that's actually what you want."
            )
        return self

    def to_env_pairs(self, prefix: str) -> list[str]:
        """Return ``PREFIX_FIELD=value`` strings for each non-None field.

        Used by :func:`~oa_configurator.io.write_env_file` to emit env vars for
        Docker Compose ``env_file:``. Field names are uppercased directly
        (e.g. ``host`` → ``PREFIX_HOST``), so adding a new field here
        automatically appears in the export without touching ``io.py``.
        The ``test_only`` config-only flag is excluded, since it is not a
        database connection parameter.
        """
        return [
            f"{prefix}_{k.upper()}={v}"
            for k, v in self.model_dump().items()
            if v is not None and k != "test_only"
        ]

    def _build_url_obj(self) -> URL:
        # Also enforced at construction time by _check_required_fields; kept
        # here as defense-in-depth for a post-construction mutated instance.
        if not requires_host(self.dialect_name):
            if not self.database_name:
                raise ValueError(
                    f"ConnectionConfig has no `database_name` set for dialect {self.dialect_name!r}"
                    " (a file-based dialect) and no longer defaults to ':memory:'. Set"
                    " `database_name` explicitly, passing ':memory:' if that's actually what you want."
                )
            return URL.create(drivername=self.dialect, database=self.database_name)
        if not self.host:
            raise ValueError(
                "ConnectionConfig has no `host` set and no longer defaults to 'localhost'."
                " Set `host` explicitly in config.toml."
            )
        return URL.create(
            drivername=self.dialect,
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database_name or "",
        )

    def build_url(self) -> str:
        """Build the full connection URL, including the plaintext password.

        Returns
        -------
        str
            SQLAlchemy-compatible connection URL. For SQLite, returns
            ``sqlite:///<database_name>``; ``database_name`` must be set
            explicitly (e.g. to ``:memory:``), there is no implicit default.
        """
        return self._build_url_obj().render_as_string(hide_password=False)

    def safe_url(self) -> str:
        """Build the connection URL with the password redacted.

        Safe for logging and display. Identical to ``build_url()`` for SQLite
        connections, which carry no password.

        Returns
        -------
        str
            Connection URL with ``***`` substituted for the password field.
        """
        return self._build_url_obj().render_as_string(hide_password=True)

    def resolve(self, name: str) -> ResolvedConnection:
        """Resolve this connection to a concrete, engine-ready target."""
        url_obj = self._build_url_obj()
        return ResolvedConnection(
            name=name,
            url=url_obj.render_as_string(hide_password=False),
            safe_url=url_obj.render_as_string(hide_password=True),
            _engine_url=url_obj,
            test_only=self.test_only,
        )

    @property
    def dialect_name(self) -> str:
        """SQLAlchemy dialect name (e.g. "postgresql", "sqlite"), derived from
        ``dialect`` alone. Unlike :attr:`ResolvedConnection.dialect_name`, this
        needs no other field set (``host``, ``database_name``, etc.), so it is
        safe to call before the connection is otherwise complete.
        """
        return URL.create(drivername=self.dialect).get_backend_name()


def _iter_schema_roles(cls: type[BaseModel]) -> Iterator[tuple[str, Role]]:
    """Yield (field_name, Role) for every Role-tagged field on cls.

    Mirrors refs._iter_refs's shape: a field's Role tag lives in its
    FieldInfo.metadata, populated from an Annotated[..., Role.X] extra.
    """
    for name, info in cls.model_fields.items():
        roles = [m for m in info.metadata if isinstance(m, Role)]
        assert len(roles) <= 1, f"{cls.__name__}.{name} has more than one Role marker"
        if roles:
            yield name, roles[0]


def _merged_schema_translate_map(
    execution_options: dict[str, Any] | None,
    configured_map: dict[str | None, str | None],
) -> dict[str, Any]:
    """Merge execution_options with the resolver's own schema_translate_map.

    A caller may add a new key create_engine() doesn't own,  e.g. a
    package's own reserved-schema role, layered on top of the CDM map. 
    A caller may NOT set a key it does own.  That's rejected with ``ValueError`` 
    to prevent silent overrides of the resolver's own schema routing. 
    """
    merged_opts = dict(execution_options or {})
    caller_map = merged_opts.pop(SCHEMA_TRANSLATE_MAP_KEY, None) or {}
    owned_conflicts = sorted(str(key) for key in caller_map if key in configured_map)
    if owned_conflicts:
        raise ValueError(
            f"execution_options[{SCHEMA_TRANSLATE_MAP_KEY!r}] must not include "
            f"resolver-managed key(s) {owned_conflicts}: create_engine() "
            "sets those from the resolved config. Extend with additional "
            "keys instead, such as a package's own reserved schema role."
        )
    merged_opts[SCHEMA_TRANSLATE_MAP_KEY] = {**caller_map, **configured_map}
    return merged_opts


class DatabaseKind(str, Enum):
    """Discriminator for :class:`DatabaseConfig` subclasses."""

    GENERIC = "generic"
    CDM = "cdm"


class DatabaseConfig(SecretSafeBaseModel):
    """Shared interface for every named database: a connection, plus each
    concrete kind's own schema field(s).

    Abstract in practice: ``kind`` has no default, so every concrete entry
    must declare it explicitly via one of the subclasses below. Use this
    class (not a subclass) for ``isinstance`` checks and ``RefTo`` targets
    that accept any kind; use :data:`DatabaseEntry` for parsing raw config
    data, which dispatches to the correct subclass based on ``kind``.

    Notes
    -----
    schema dispatch is handled in subclasses. See :class:`GenericDatabaseConfig`
    for `schema_name` and :class:`CDMDatabaseConfig` for `cdm_schema`,
    `vocab_schema`, and `results_schema`. 

    """

    model_config = ConfigDict(extra="forbid")

    kind: DatabaseKind = Field(description="Which concrete database shape this entry is.")
    connection: Annotated[str, RefTo(ConnectionConfig)] = Field(
        description="Name of the connection entry (from [connections]) used as the primary server."
    )

    def connection_name_for_role(self, role: Role) -> str:
        """Name of the connection entry to use for role.

        Always self.connection here; overridden by CDMDatabaseConfig, whose
        vocab role may route to a separate connection.
        """
        return self.connection


class GenericDatabaseConfig(DatabaseConfig):
    """A connection plus one optional schema. No CDM role-splitting.

    Used by consumers that need a single database with no vocab/results
    distinction, e.g. an embedding store or a metadata database.
    """

    kind: Literal[DatabaseKind.GENERIC] = DatabaseKind.GENERIC  # type: ignore[assignment]
    schema_name: Annotated[str | None, Role.PRIMARY] = Field(
        default=None,
        description=(
            "Schema this database's tables live in. None means no override, use the "
            "connection's own default/search_path."
        ),
    )

    def resolve(self, name: str, stack: StackConfig) -> ResolvedDatabase:
        """Resolve this database to a concrete connection and effective schema.

        *stack* must already have passed :meth:`StackConfig.validate_references`,
        so ``self.connection`` is guaranteed to exist in ``stack.connections``.

        Raises
        ------
        RuntimeError
            If ``schema_name`` collides with a schema reserved for internal
            bookkeeping (see :func:`~.sql.register_reserved_schema`).
        """
        reject_reserved_schema(self.schema_name)
        primary = stack.connections[self.connection].resolve(self.connection)
        return ResolvedDatabase(name=name, connection=primary, schema_name=self.schema_name)


class CDMDatabaseConfig(DatabaseConfig):
    """Maps the OMOP logical roles (CDM, vocab, results) to named connections and schema names.

    This is what a CDM-consuming package treats as "its database": the
    logical CDM/vocab/results bundle, as opposed to :class:`ConnectionConfig`
    (the physical server address and credentials underneath it). Most
    packages only need a single ``cdm_db`` database.
    """

    kind: Literal[DatabaseKind.CDM] = DatabaseKind.CDM  # type: ignore[assignment]
    cdm_schema: Annotated[str | None, Role.PRIMARY] = Field(
        default=None,
        description="Schema where CDM clinical tables live. None means no override, use the connection's own default/search_path.",
    )
    vocab_connection: Annotated[str | None, RefTo(ConnectionConfig)] = Field(
        default=None,
        description="Name of the connection entry for vocabulary tables. Falls back to connection when not set.",
    )
    vocab_schema: Annotated[str | None, Role.VOCAB] = Field(
        default=None,
        description="Vocabulary schema. Falls back to cdm_schema when not set.",
    )
    results_schema: Annotated[str | None, Role.RESULTS] = Field(
        default=None,
        description="Achilles / Atlas results schema. Falls back to cdm_schema when not set.",
    )

    def connection_name_for_role(self, role: Role) -> str:
        """Name of the connection entry to use for role: vocab_connection
        for Role.VOCAB when explicitly configured, else the primary connection.
        """
        if role == Role.VOCAB and self.vocab_connection is not None:
            return self.vocab_connection
        return self.connection

    def _schema_for_role(self, role: Role) -> str | None:
        """Effective config-time schema value for role: the role's own
        Role-tagged field if explicitly set, else the field tagged Role.PRIMARY.
        """
        field_by_role = {r: name for name, r in _iter_schema_roles(type(self))}
        if role in field_by_role:
            value = getattr(self, field_by_role[role])
            if value is not None:
                return value
        return getattr(self, field_by_role[Role.PRIMARY])

    def resolve(self, name: str, stack: StackConfig) -> ResolvedCDMDatabase:
        """Resolve this database to concrete connections and effective schema names.

        The vocab connection falls back to the primary connection when not
        explicitly configured; the vocab schema falls back to the CDM
        schema under the same condition. ``vocab_schema``/``results_schema``
        resolve to ``None`` when their own connection's dialect has no real
        multi-schema concept (e.g. SQLite), rather than carrying a
        dialect-inappropriate string that a later fold would need to
        correct. *stack* must already have passed
        :meth:`StackConfig.validate_references`, so ``self.connection``/
        ``self.vocab_connection`` are guaranteed to exist in
        ``stack.connections``.

        Raises
        ------
        RuntimeError
            If ``cdm_schema``, ``vocab_schema``, or ``results_schema``
            collides with a schema reserved for internal bookkeeping (see
            :func:`~.sql.register_reserved_schema`).
        """
        effective_vocab_schema = self._schema_for_role(Role.VOCAB)
        effective_results_schema = self._schema_for_role(Role.RESULTS)
        reject_reserved_schema(self.cdm_schema)
        reject_reserved_schema(effective_vocab_schema)
        reject_reserved_schema(effective_results_schema)
        primary_connection_name = self.connection_name_for_role(Role.PRIMARY)
        primary_connection = stack.connections[primary_connection_name].resolve(primary_connection_name)
        vocab_connection_name = self.connection_name_for_role(Role.VOCAB)
        vocab_connection = (
            primary_connection
            if vocab_connection_name == primary_connection_name
            else stack.connections[vocab_connection_name].resolve(vocab_connection_name)
        )
        if not supports_schemas(vocab_connection.dialect_name):
            effective_vocab_schema = None
        if not supports_schemas(primary_connection.dialect_name):
            effective_results_schema = None
        return ResolvedCDMDatabase(
            name=name,
            connection=primary_connection,
            schema_name=self.cdm_schema,
            vocab_connection=vocab_connection,
            vocab_schema=effective_vocab_schema,
            results_schema=effective_results_schema,
        )


DatabaseEntry = Annotated[
    GenericDatabaseConfig | CDMDatabaseConfig,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class ResolvedConnection:
    """Concrete physical connection ready for engine creation.

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
    _engine_url : sqlalchemy.engine.URL
        SQLAlchemy URL object used for engine creation. Avoids the lossy string
        round-trip through ``url`` for SQLite paths containing ``?``/``#``.
    test_only : bool
        Whether it is a test-only connection. 
    """

    name: str
    url: str
    safe_url: str
    _engine_url: URL = field(repr=False, compare=False)
    test_only: bool = False

    def create_engine(self, **kwargs: Any) -> Engine:
        """Create a SQLAlchemy engine for this connection.

        Parameters
        ----------
        **kwargs
            Forwarded to ``sqlalchemy.create_engine``. ``pool_pre_ping``
            defaults to ``True``. Pass ``pool_pre_ping=False`` to opt out.

        Notes
        -----
        ``pool_pre_ping=True`` checks a pooled connection is still alive
        before handing it out, avoiding stale-connection failures after
        a long-idle period. It adds a small overhead to every checkout,
        so it can be disabled when the database is known to be reliable 
        and the application is latency-sensitive using ``**kwargs``.

        Returns
        -------
        sqlalchemy.engine.Engine
        """
        kwargs.setdefault("pool_pre_ping", True)
        return sa.create_engine(self._engine_url, **kwargs)

    @property
    def dialect_name(self) -> str:
        """SQLAlchemy dialect name (e.g. "postgresql", "sqlite"), read off
        the URL directly.
        """
        return self._engine_url.get_backend_name()

    def __repr__(self) -> str:
        return f"ResolvedConnection(name={self.name!r}, safe_url={self.safe_url!r})"


@dataclass(frozen=True)
class ResolvedDatabase:
    """Resolved generic database: one connection, one optional schema.

    Attributes
    ----------
    name : str
        Logical name of the database as declared in the config.
    connection : ResolvedConnection
        Resolved connection for this database.
    schema_name : str | None
        Effective schema name for this database, or None for no override
        (use the connection's own default/search_path).
    """

    name: str
    connection: ResolvedConnection
    schema_name: str | None

    def schema_translate_map(self) -> dict[str | None, str | None]:
        """SQLAlchemy schema translate map for this database.
        Routing:
            - ``"primary"`` → ``schema_name`` (or None if the dialect 
                has no real multi-schema concept, e.g. SQLite)
            
        Notes
        -----
        An untagged SQLAlchemy table (``schema=None``) is not redirected here
        and falls back to the connection's own default/search_path.
        """
        return {
            Role.PRIMARY.value: schema_if_supported(self.schema_name, self.connection.dialect_name),
        }

    def create_engine(
        self,
        role: Role = Role.PRIMARY,
        *,
        execution_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Engine:
        """Create a SQLAlchemy engine with the schema translate map applied.

        Parameters
        ----------
        role : Role, optional
            Which connection to create an engine for. Only ``Role.PRIMARY``
            is valid here; anything else raises as a ResolvedDatabase has no
            vocab/results connection-splitting. Defaults to ``Role.PRIMARY``.
        execution_options : dict, optional
            Additional execution options merged into the engine. A
            ``schema_translate_map`` here may add keys the resolver doesn't
            define, but may not include ``"primary"`` (the resolver's own key):
            that key is always set from the resolved config, and overriding
            it here would silently defeat the configured schema routing.
        **kwargs
            Forwarded to ``sqlalchemy.create_engine``.

        Returns
        -------
        sqlalchemy.engine.Engine
            Engine configured with :meth:`schema_translate_map`.

        Raises
        ------
        ValueError
            If ``execution_options['schema_translate_map']`` includes any resolver-
            managed keys.
        RuntimeError
            If ``schema_name`` collides with a reserved schema. Normally
            already caught by :meth:`DatabaseConfig.resolve`; repeated here
            as defense in depth for a hand-built ``ResolvedDatabase`` that
            skipped ``.resolve()``.
        """
        reject_reserved_schema(self.schema_name)
        engine = self.connection_for_role(role).create_engine(**kwargs)
        merged_opts = _merged_schema_translate_map(execution_options, self.schema_translate_map())
        return engine.execution_options(**merged_opts)

    def connection_for_role(self, role: Role = Role.PRIMARY) -> ResolvedConnection:
        """Return the resolved connection for a given role.
        Only Role.PRIMARY is valid here as ResolvedDatabase has no vocab/results connection-splitting.

        Raises
        ------
        ValueError
            If *role* is not ``Role.PRIMARY``.
        """
        if role != Role.PRIMARY:
            raise ValueError(
                f"Role.{role.name} has no meaning for {type(self).__name__}; "
                "only ResolvedCDMDatabase has vocab/results connections."
            )
        return self.connection

    def schema_for_role(self, role: Role = Role.PRIMARY) -> str | None:
        """Return the effective schema for a given role.
        Only PRIMARY is valid here as ResolvedDatabase has no vocab/results role-splitting.

        Raises
        ------
        ValueError
            If *role* is not ``Role.PRIMARY``.
        """
        if role != Role.PRIMARY:
            raise ValueError(
                f"Role.{role.name} has no meaning for {type(self).__name__}; "
                "only ResolvedCDMDatabase has vocab/results roles."
            )
        return self.schema_name

    def schema_tags(self) -> tuple[Role, ...]:
        """Role tags whose schema provenance is worth tracking for this database."""
        return (Role.PRIMARY,)

    def occupied_schemas(self, connection: sa.Connection) -> set[str]:
        """Physical schema names this database currently claims.

        Reads schema_translate_map() rather than self.schema_name directly,
        so the supports_schemas fold it already applies isn't reimplemented
        here. Unlike schema_translate_map() itself, an unset entry resolves
        to the connection's own live default schema (e.g. "public"), not
        None: this reports the real physical schema a table lands in, not
        a translate-map directive.

        Parameters
        ----------
        connection : sqlalchemy.engine.Connection
            Open connection to this database's own server, used to read its
            live default schema (search_path-dependent, not a static
            per-dialect guess). The caller is expected to already have one
            in scope, not open a fresh one just for this.
        """
        default = sa.inspect(connection).default_schema_name
        schema = self.schema_translate_map()[Role.PRIMARY.value] or default
        return {schema} if schema is not None else set()

    def __repr__(self) -> str:
        return (
            f"ResolvedDatabase(name={self.name!r}, "
            f"connection={self.connection.name!r}, "
            f"schema_name={self.schema_name!r})"
        )


@dataclass(frozen=True)
class ResolvedCDMDatabase(ResolvedDatabase):
    """Resolved CDM database: adds vocab/results role-splitting on top of
    :class:`ResolvedDatabase`.

    Attributes
    ----------
    vocab_connection : ResolvedConnection
        Resolved vocabulary connection for this database. May be the same as
        *connection* if no separate vocab connection is configured.
    vocab_schema : str or None
        Effective vocabulary schema name for this database. May be the same
        as schema_name if no separate vocab schema is configured; None when
        vocab_connection's dialect has no real multi-schema concept (e.g.
        SQLite).
    results_schema : str or None
        Effective results schema name for this database. May be the same as
        schema_name if no separate results schema is configured; None when
        connection's dialect has no real multi-schema concept (e.g. SQLite).
    """

    vocab_connection: ResolvedConnection
    vocab_schema: str | None
    results_schema: str | None

    @property
    def cdm_schema(self) -> str | None:
        """Alias for ``schema_name``, matching OHDSI's own CDM/VOCAB/RESULTS naming."""
        return self.schema_name

    def connection_for_role(self, role: Role = Role.PRIMARY) -> ResolvedConnection:
        """Return the resolved connection for a given role: only ``Role.PRIMARY``
        or ``Role.VOCAB``, the only two physical connections that exist.

        Raises
        ------
        ValueError
            If *role* is neither ``Role.PRIMARY`` nor ``Role.VOCAB``.
        """
        if role == Role.VOCAB:
            return self.vocab_connection
        if role == Role.PRIMARY:
            return self.connection
        raise ValueError(
            f"Role.{role.name} is not a valid connection role for {type(self).__name__}; "
            "only Role.PRIMARY and Role.VOCAB select a connection."
        )

    def schema_for_role(self, role: Role = Role.PRIMARY) -> str | None:
        """Return the effective schema for a given role.
        See ~meth:`CDMDatabaseConfig.resolve` for how vocab/results roles are handled.

        Parameters
        ----------
        role : Role, optional
            Which schema to return. Defaults to ``Role.PRIMARY``.

        Returns
        -------
        str or None
            ``vocab_schema`` for ``Role.VOCAB``, ``results_schema`` for
            ``Role.RESULTS``, ``schema_name`` for ``Role.PRIMARY``.

        Raises
        ------
        ValueError
            If role is not recognised as a Role member. 
        """
        if role == Role.VOCAB:
            return self.vocab_schema
        if role == Role.RESULTS:
            return self.results_schema
        if role == Role.PRIMARY:
            return self.schema_name
        raise ValueError(f"Role.{role.name} is not handled by {type(self).__name__}.schema_for_role.")

    def schema_translate_map(self) -> dict[str | None, str | None]:
        """SQLAlchemy schema translate map for OMOP ORM models.

        Routing:
          "primary" → cdm_schema
          "vocab"   → vocab_schema, falling back to cdm_schema if unset
          "results" → results_schema, falling back to cdm_schema if unset

        Notes
        -----
        If cdm_schema itself is unset, "primary" falls through to the
        connection's own default/search_path. A genuinely untagged SQLAlchemy 
        table (``schema=None``) is never redirected here at all.

        Each key folds to ``None`` on a dialect with no real multi-schema
        concept (e.g. SQLite). "vocab" checks ``vocab_connection``'s own
        dialect, since that can genuinely be a separate connection;
        "primary"/"results" both check ``connection``'s dialect, since
        neither has a connection of its own.
        """
        return {
            Role.PRIMARY.value: schema_if_supported(self.schema_name, self.connection.dialect_name),
            Role.VOCAB.value: schema_if_supported(self.vocab_schema, self.vocab_connection.dialect_name),
            Role.RESULTS.value: schema_if_supported(self.results_schema, self.connection.dialect_name),
        }

    def schema_tags(self) -> tuple[Role, ...]:
        """Role tags whose schema provenance is worth tracking: primary, vocab, and results."""
        return (Role.PRIMARY, Role.VOCAB, Role.RESULTS)

    def occupied_schemas(self, connection: sa.Connection) -> set[str]:
        """Physical schema names this database currently claims.
        If the vocab_connection is a genuinely different connection 
        than connection, opens a short-lived connection to it just to 
        read its default schema, then disposes it immediately.
        """
        schemas = super().occupied_schemas(connection)
        default = sa.inspect(connection).default_schema_name
        if self.connection == self.vocab_connection:
            vocab_default = default
        else:
            vocab_engine = self.vocab_connection.create_engine()
            try:
                with vocab_engine.connect() as vocab_connection:
                    vocab_default = sa.inspect(vocab_connection).default_schema_name
            finally:
                vocab_engine.dispose()
        translated = self.schema_translate_map()
        for schema in (
            translated[Role.VOCAB.value] or vocab_default,
            translated[Role.RESULTS.value] or default,
        ):
            if schema is not None:
                schemas.add(schema)
        return schemas

    def create_engine(
        self,
        role: Role = Role.PRIMARY,
        *,
        execution_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Engine:
        """Create a SQLAlchemy engine with the schema translate map applied.

        The schema translate map routes OMOP ORM models to the correct schemas
        automatically (``"primary"`` -> schema_name, ``"vocab"`` -> vocab_schema,
        ``"results"`` -> results_schema).

        Parameters
        ----------
        role : Role, optional
            Which connection to create an engine for. Defaults to
            ``Role.PRIMARY``.
        execution_options : dict, optional
            Additional execution options merged into the engine.
            Additional execution options merged into the engine. A
            ``schema_translate_map`` here may add keys the resolver doesn't
            define, but may not include resolver-managed keys to prevent
            silent overwrites.
        **kwargs
            Forwarded to ``sqlalchemy.create_engine``.

        Returns
        -------
        sqlalchemy.engine.Engine
            Engine configured with :meth:`schema_translate_map` for OMOP ORM routing.

        Raises
        ------
        ValueError
            If ``execution_options['schema_translate_map']`` includes any
            resolver-managed keys.
        RuntimeError
            If ``cdm_schema``, ``vocab_schema``, or ``results_schema``
            collides with a reserved schema. Normally already caught by
            :meth:`CDMDatabaseConfig.resolve`; repeated here as defense in
            depth for a hand-built ``ResolvedCDMDatabase`` that skipped
            ``.resolve()``. Does not call ``ResolvedDatabase.create_engine``
            (this override builds its own engine via ``connection_for_role``),
            so that check doesn't run here for free and needs repeating.
        """
        reject_reserved_schema(self.vocab_schema)
        reject_reserved_schema(self.results_schema)
        return super().create_engine(role=role, execution_options=execution_options, **kwargs)

    def vocab_engine_for(
        self,
        primary: Engine,
        *,
        execution_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Engine:
        """Return the vocab-role engine paired with an already-built ``primary`` engine.

        Returns ``primary`` unchanged when ``vocab_connection`` is not a genuinely
        different connection, avoiding a second, redundant connection pool
        to the same target.
        """
        if self.connection == self.vocab_connection:
            return primary
        return self.create_engine(
            role=Role.VOCAB, execution_options=execution_options, **kwargs
        )

    def create_engines(
        self,
        *,
        execution_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[Engine, Engine]:
        """Return ``(primary_engine, vocab_engine)``, built together.

        See :meth:`vocab_engine_for` for the pairing rule. Parameters are
        forwarded to :meth:`create_engine` for both.
        """
        primary = self.create_engine(execution_options=execution_options, **kwargs)
        vocab = self.vocab_engine_for(primary, execution_options=execution_options, **kwargs)
        return primary, vocab

    def __repr__(self) -> str:
        return (
            f"ResolvedCDMDatabase(name={self.name!r}, "
            f"connection={self.connection.name!r}, "
            f"schema_name={self.schema_name!r}, "
            f"vocab_schema={self.vocab_schema!r}, "
            f"results_schema={self.results_schema!r})"
        )
