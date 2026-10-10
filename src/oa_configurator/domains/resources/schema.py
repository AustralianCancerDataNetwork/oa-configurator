"""Resources domain: physical connections and logical CDM/vocab/results database bundles."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
from typing import TYPE_CHECKING, Annotated, Any, Literal, NamedTuple, TypeVar

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import event
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.sql import visitors

from ...refs import RefTo, Secret, SecretSafeBaseModel
from .schema_registry import (
    _SCHEMA_PROVENANCE_SCHEMA,
    CrossDatabaseStatementError,
    _check_schema_claim,
    _ensure_schema_registry_table,
    _guard_schema_provenance,
    _lock_schema_registry,
    _register_schema_claim,
    physical_schema_of,
)
from .sql import (
    EXECUTION_OPTION_DATABASE_CONFIG_NAME,
    EXECUTION_OPTION_TEST_ONLY,
    SCHEMA_TRANSLATE_MAP_KEY,
    Bindable,
    Role,
    connection_key,
    ensure_schema,
    is_ephemeral_url,
    requires_host,
    schema_if_supported,
    supports_schemas,
)

MIGRATION_COMMAND = (
    "uv run https://raw.githubusercontent.com/AustralianCancerDataNetwork/oa-configurator/"
    "main/migrations/to_v2.py"
)

UPGRADING_GUIDE_URL = "https://AustralianCancerDataNetwork.github.io/oa-configurator/upgrading-from-1.x/"

if TYPE_CHECKING:
    from ...stack_config import StackConfig

# Types for route_for_schema_tag()
_T = TypeVar("_T")

class SchemaClaim(NamedTuple):
    """Explicit entry to the schema_translate_map passed to create_engine().

    Attributes
    ----------
    schema_tag : str
        The ``schema_translate_map`` key this claim registers.
    physical_schema : str or None
        The physical schema this claim resolves to. ``None`` is folded
        away entirely for a dialect with no schema concept; see
        ``create_engine()``.
    reserved : bool, optional
        True if *physical_schema* may not be used by any other owner on
        this connection, regardless of ``schema_tag``.
    owner : str, optional
        Explicit owner for this one claim, overriding create_engine()'s
        own owner (stack-derived or explicit). Only needed for a caller
        aggregating several packages' claims into one create_engine() call,
        where the immediate caller isn't the true owner of everything in it.
    """

    schema_tag: str
    physical_schema: str | None
    reserved: bool = False
    owner: str | None = None

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

    def physical_key(self) -> str:
        """Physical identity of the database this connection addresses.

        See :func:`connection_key`, which this defers to so a config entry
        and a live URL compare through one implementation.
        """
        return connection_key(self._build_url_obj())

    def physical_identity(self) -> tuple[Any, ...]:
        """Identity tuple (physical connection key, user) used to decide if
        this connection is the same physical connection as another one.

        ``user`` is a separate element so callers comparing addresses alone
        can drop it.
        """
        return (self.physical_key(), self.user)


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


def _derive_owner() -> str | None:
    """Top-level package name of the first call-stack frame outside oa_configurator
    and contextlib. To be used in combination with create_engine()."""

    frame = inspect.currentframe()
    frame = frame.f_back if frame is not None else None
    while frame is not None:
        module_name = frame.f_globals.get("__name__", "")
        if module_name and not module_name.startswith("oa_configurator") and module_name != "contextlib":
            return module_name.split(".")[0]
        frame = frame.f_back
    return None


def statement_schema_tags(clause: Any) -> set[str]:
    """Schema tags of every ``Table`` appearing anywhere in *clause*.

    Reads the in-Python ``Table.schema``, which carries the tag rather than
    the physical schema, so this sees what a statement asks for before
    ``schema_translate_map`` rewrites it. A raw ``text()`` construct carries
    no table metadata and yields an empty set.
    """
    return {
        element.schema
        for element in visitors.iterate(clause)
        if isinstance(element, sa.Table) and element.schema
    }


def _install_cross_database_guard(engine: Engine, resolved: ResolvedDatabase) -> None:
    """Refuse statements on *engine* that reference a role hosted on another database.

    Covers a join spanning two databases and a statement sent to the wrong
    engine alike, since both reference a role this engine does not host.
    Only resolver role tags are checked: a caller-claimed tag is hosted by
    whichever engine claimed it.

    Installed on the engine rather than at call sites, so every consumer
    inherits the protection without opting in. Only attached when *resolved*
    actually splits its tags across connections, leaving a colocated
    deployment at no cost.
    """
    role_tags = {role.value for role in resolved.schema_tags()}
    hosted = {role.value for role in resolved.roles_on_connection(engine)}

    @event.listens_for(engine, "before_execute")
    def _guard(  # type: ignore[misc]
        conn: Connection,
        clauseelement: Any,
        multiparams: Any,
        params: Any,
        execution_options: Any,
    ) -> None:
        foreign = sorted((statement_schema_tags(clauseelement) & role_tags) - hosted)
        if not foreign:
            return
        hosts = {tag: resolved.connection_for_schema_tag(tag).name for tag in foreign}
        raise CrossDatabaseStatementError(
            f"Statement references schema tag(s) {foreign} hosted on another database "
            f"({hosts}), but runs on an engine for connection "
            f"{resolved.connection_for_schema_tag(next(iter(hosted))).name!r}. "
            "Run it on an engine that hosts all referenced tables, or split it into "
            "database-local operations."
        )


def referred_schema_tag(foreign_key: sa.ForeignKey, default: str) -> str:
    """Schema tag a foreign key points at, or *default* when it names none.

    Read from ``target_fullname`` rather than from ``fk.column``, which
    resolves the referenced table and raises on a copied table whose target
    is not in the same MetaData.
    """
    parts = foreign_key.target_fullname.split(".")
    return parts[0] if len(parts) == 3 else default


def without_cross_engine_foreign_keys(
    tables: Iterable[sa.Table],
    *,
    resolved: ResolvedDatabase,
) -> list[sa.Table]:
    """Copies of *tables* with foreign keys that cross a database boundary removed.

    Derived from the real tables so a column or type added to a model cannot
    drift from its copy. Only constraints whose referred tag is hosted on
    another connection are dropped; a composite key targeting the same tag
    survives untouched.

    This changes only the DDL emitted for a split deployment. The ORM models
    keep their own ``ForeignKey`` declarations, and SQLAlchemy infers join
    conditions from those in-Python, so relationship loading does not depend
    on the physical constraint existing.

    Parameters
    ----------
    tables : Iterable[sqlalchemy.Table]
        Tables to copy, typically everything one engine creates. Each table's
        own ``schema`` is the tag it belongs to.
    resolved : ResolvedDatabase
        Topology answering whether a foreign key can span two tags.

    Returns
    -------
    list[sqlalchemy.Table]
        The copies, in input order, all in one fresh MetaData so a surviving
        foreign key resolves its target at DDL compilation. The originals
        and their MetaData are never mutated.
    """
    target = sa.MetaData()
    copies = [table.to_metadata(target) for table in tables]
    for copied in copies:
        own_tag = copied.schema
        if own_tag is None:
            continue
        for constraint in [
            candidate
            for candidate in copied.constraints
            if isinstance(candidate, sa.ForeignKeyConstraint)
        ]:
            crosses = any(
                not resolved.foreign_key_can_span(
                    own_tag, referred_schema_tag(element, own_tag)
                )
                for element in constraint.elements
            )
            if not crosses:
                continue
            copied.constraints.discard(constraint)
            for element in constraint.elements:
                copied.foreign_keys.discard(element)
                element.parent.foreign_keys.discard(element)
    return copies


def _process_schema_claims(
    engine: Engine,
    claims: Sequence[SchemaClaim],
    *,
    database_config_name: str,
    translate_only: Sequence[SchemaClaim] = (),
    test_only: bool = False,
    register_claims: bool = True,
) -> dict[str, str | None]:
    """Fold, register, and translate every claim in one pass.
    No-op on a dialect with no real multi-schema concept.

    With ``register_claims=False``, claims are only checked for ownership and
    reservation conflicts; nothing is written to the schema_registry.

    Parameters
    ----------
    translate_only : Sequence[SchemaClaim], optional
        Claims folded into the returned translate_map without being
        registered or checked, for a schema living on a connection other
        than this engine's (a split CDM's primary and results tags, seen
        while building the vocab-only engine). Their ``physical_schema``
        is taken verbatim, ``None`` included, since this engine's default
        schema is not the one they resolve against.

    Notes
    -----
    - ``physical_schema=None`` on a registered claim resolves to this
    connection's own real default schema, read over the shared connection.
    - Each claim's ``owner`` is used exactly as given, so a claim left
    deliberately unowned stays unowned.
    - An own-built engine and a SQLite engine end up in the same, unchecked
    state deliberately, since neither has anything real to protect.
    - A dialect without real multi-schema support (e.g. SQLite) automatically
    folds every schema_tag to None.

    Returns
    -------
    dict[str, str | None]
        The dict to set as ``execution_options[schema_translate_map]``.
    """
    if not supports_schemas(engine):
        return {claim.schema_tag: None for claim in (*translate_only, *claims)}

    translate_map: dict[str, str | None] = {
        claim.schema_tag: claim.physical_schema for claim in translate_only
    }
    # Forces READ COMMITTED on the registration connection so a caller's
    # AUTOCOMMIT isolation level can't defeat _lock_schema_registry's lock.
    connection_cm = (
        engine.connect().execution_options(isolation_level="READ COMMITTED")
        if register_claims
        else engine.connect()
    )
    with connection_cm as connection:
        transaction = connection.begin() if register_claims else None
        try:
            if register_claims:
                _lock_schema_registry(connection)
                connection = _ensure_schema_registry_table(connection)
            for claim in claims:
                physical_schema = (
                    claim.physical_schema
                    if claim.physical_schema is not None
                    else sa.inspect(connection).default_schema_name
                )
                translate_map[claim.schema_tag] = physical_schema
                if register_claims:
                    _register_schema_claim(
                        connection,
                        database_config_name=database_config_name,
                        schema_tag=claim.schema_tag,
                        physical_schema=physical_schema,
                        owner=claim.owner,
                        reserved=claim.reserved,
                        test_only=test_only,
                    )
                    ensure_schema(connection, physical_schema)
                else:
                    _check_schema_claim(
                        connection,
                        database_config_name=database_config_name,
                        schema_tag=claim.schema_tag,
                        physical_schema=physical_schema,
                        owner=claim.owner,
                        reserved=claim.reserved,
                        test_only=test_only,
                    )
            if transaction is not None:
                transaction.commit()
        except BaseException:
            if transaction is not None:
                transaction.rollback()
            raise
    return translate_map


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
        """
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

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_schema_name(cls, value: Any) -> Any:
        if isinstance(value, dict) and "schema_name" in value:
            raise ValueError(
                "This looks like a 1.x config: `schema_name` on a `kind = \"cdm\"` "
                "entry became `cdm_schema` in 2.0, whose default is now None. "
                f"Follow the manual upgrade steps at {UPGRADING_GUIDE_URL}. "
                f"(A migration script will also be available via `{MIGRATION_COMMAND}`.)"
            )
        return value

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
        """
        effective_vocab_schema = self._schema_for_role(Role.VOCAB)
        effective_results_schema = self._schema_for_role(Role.RESULTS)
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

    def physical_key(self) -> str:
        """Physical identity of the database this connection addresses.

        See :func:`connection_key`. Two connection entries with different
        names that address one database share a key, which is what decides
        whether a role is genuinely hosted elsewhere.
        """
        return connection_key(self._engine_url)

    def addresses_same_database_as(self, other: ResolvedConnection) -> bool:
        """Do self and other address the same physical database?

        Compares physical identity rather than dataclass equality, so a
        second config entry pointing at one database is recognised as the
        same database rather than as a separate one.
        """
        if is_ephemeral_url(self._engine_url) or is_ephemeral_url(other._engine_url):
            return False
        return self.physical_key() == other.physical_key()

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
class _SchemaProbeOptions:
    built_engine: Engine | None = None
    built_connection: ResolvedConnection | None = None
    extensions: Sequence[Callable[[Any, Any], None]] = ()
    engine_kwargs: dict[str, Any] = field(default_factory=dict)


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

    def configured_internal_schema_translate_map(self) -> dict[str, str | None]:
        """Schema translate map for this database.
        Routing:
            - ``"primary"`` → ``schema_name`` (or None if the dialect 
                has no real multi-schema concept, e.g. SQLite)
            
        Notes
        -----
        This is NOT the full schema_translate_map for an engine!
        See :meth:`create_engine` for the full map, which merges the resolver's own 
        config-derived claims with any additional claims passed in by the caller.
        """
        return {
            Role.PRIMARY.value: schema_if_supported(self.schema_name, self.connection.dialect_name),
        }

    def create_engine(
        self,
        *,
        schema_claims: Iterable[SchemaClaim] = (),
        extensions: Sequence[Callable[[Any, Any], None]] = (),
        execution_options: dict[str, Any] | None = None,
        owner: str | None = None,
        register_claims: bool = True,
        **kwargs: Any,
    ) -> Engine:
        """Create a SQLAlchemy engine with the schema translate map applied.

        Parameters are as for :meth:`_build_engine`, which this calls for
        the primary connection.
        """
        return self._build_engine(
            Role.PRIMARY,
            schema_claims=schema_claims,
            extensions=extensions,
            execution_options=execution_options,
            owner=owner,
            register_claims=register_claims,
            **kwargs,
        )

    def _build_engine(
        self,
        role: Role = Role.PRIMARY,
        *,
        schema_claims: Iterable[SchemaClaim] = (),
        extensions: Sequence[Callable[[Any, Any], None]] = (),
        execution_options: dict[str, Any] | None = None,
        owner: str | None = None,
        register_claims: bool = True,
        **kwargs: Any,
    ) -> Engine:
        """Build the engine for *role*'s connection with the schema translate map applied.

        Notes
        -----
        Checks schema_translate_map construction. The resolver's own config-derived
        claims are always injected and merged with the *schema_claims* passed here.

        The schema_registry table itself is never created here directly;
        each claim is dispatched to _register_schema_claim(), which checks
        and creates/updates the schema_registry table as needed.

        Parameters
        ----------
        role : Role, optional
            Which connection to create an engine for.
        schema_claims : Iterable[SchemaClaim], optional
            Additional schema claims to register and fold into the schema_translate_map.
            May not use a resolver-managed schema_tag.
        extensions : Sequence[Callable[[Any, Any], None]], optional
            Callables matching SQLAlchemy's ``"connect"`` event signature,
            ``(dbapi_connection, connection_record) -> None``, each attached to the
            engine before any connection is opened . Use this to load a database extension a
            backend needs on every physical connection, e.g. loading a SQLite
            extension or running ``CREATE EXTENSION IF NOT EXISTS`` on Postgres.

            Each callable runs on every new connection this engine's pool ever
            creates for the engine's entire lifetime. It must therefore be
            idempotent and cheap enough to repeat indefinitely.
        execution_options : dict, optional
            Additional, non-schema execution options merged into the
            engine (e.g. ``isolation_level``). May not include
            ``schema_translate_map`` -> pass *schema_claims* instead.
        owner : str, optional
            Name of the package registering the schema claims.
            If omitted, derived automatically from the caller's own module.
            Useful if the immediate caller isn't the true owner of everything
            it's registering (e.g. aggregating several packages' tags into one call).
        register_claims : bool, optional
            If False, claims are only checked for ownership and reservation
            conflicts and nothing is written to the schema_registry (e.g. for
            read-only diagnostics). Defaults to True.
        **kwargs
            Forwarded to ``sqlalchemy.create_engine``.

        Returns
        -------
        sqlalchemy.engine.Engine
            Engine configured with the merged schema_translate_map.

        Raises
        ------
        ValueError
            If ``execution_options`` includes ``schema_translate_map``, or
            *schema_claims* reuses a resolver-managed schema_tag.
        oa_configurator.domains.resources.sql.SchemaOwnershipError
            If a different owner already claims one of these schema tags, or
            a claim conflicts with a reservation on this connection.
        oa_configurator.domains.resources.sql.SchemaDriftError
            If an existing baseline row for a claim's schema_tag disagrees
            with its currently-resolved physical schema. Raised whether
            *register_claims* is True (checked during registration) or False
            (checked without writing), for any connection that isn't
            test_only.
        """
        if execution_options and SCHEMA_TRANSLATE_MAP_KEY in execution_options:
            raise ValueError(
                f"execution_options must not include {SCHEMA_TRANSLATE_MAP_KEY!r}. "
                "Utilise schema_claims instead to pass your additional schema_translate_map entries."
            )

        # Resolver-managed tags are known purely from self and the caller's own
        # schema_claims argument, checked before anything touches a real
        # connection, so a caller error is reported without needing a live database.
        owned_tags = {_SCHEMA_PROVENANCE_SCHEMA, *self.configured_internal_schema_translate_map()}
        # Derived before anything else so the stack walk sees the real caller's
        # module rather than a frame further inside create_engine().
        default_owner = owner if owner is not None else _derive_owner()
        caller_claims = [
            claim if claim.owner is not None else claim._replace(owner=default_owner)
            for claim in schema_claims
        ]
        conflicts = sorted(
            claim.schema_tag for claim in caller_claims if claim.schema_tag in owned_tags
        )
        role_tags = {role.value for role in Role}
        invalid_role_tags = sorted(
            claim.schema_tag
            for claim in caller_claims
            if claim.schema_tag in role_tags
        )
        if invalid_role_tags and not isinstance(self, ResolvedCDMDatabase):
            raise ValueError(
                f"schema_claims must not use CDM role tag(s) {invalid_role_tags} on a generic database."
            )
        if conflicts:
            raise ValueError(
                f"schema_claims must not include resolver-managed schema_tag(s) {conflicts}: "
                "create_engine() derives those from the resolved config."
            )

        engine = self.connection_for_role(role).create_engine(
            execution_options=execution_options, **kwargs
        )

        for extension in extensions:
            event.listens_for(engine, "connect")(extension)

        if not self.tags_share_a_transaction(*(r.value for r in self.schema_tags())):
            _install_cross_database_guard(engine, self)

        schema_provenance_claim = SchemaClaim(
            schema_tag=_SCHEMA_PROVENANCE_SCHEMA,
            physical_schema=schema_if_supported(_SCHEMA_PROVENANCE_SCHEMA, engine),
            reserved=True,
            owner="oa_configurator",
        )
        # Keyed by Role rather than by tag string, so a role hosted elsewhere is
        # told apart from a caller claim that happens to share its spelling.
        # Schemas come pre-resolved so a role hosted on another connection
        # carries that connection's own default rather than this engine's.
        internal_map = self.resolved_physical_schemas(
            probe_options=_SchemaProbeOptions(
                built_engine=engine,
                built_connection=self.connection_for_role(role),
                extensions=extensions,
                engine_kwargs=kwargs,
            ),
        )
        local_roles = self.roles_on_connection(engine)
        role_claims = {
            schema_role: SchemaClaim(
                schema_tag=schema_role.value,
                physical_schema=internal_map[schema_role.value],
            )
            for schema_role in self.schema_tags()
        }

        connection_test_only = self.connection_for_role(role).test_only
        translate_map = _process_schema_claims(
            engine,
            [
                schema_provenance_claim,
                *(claim for r, claim in role_claims.items() if r in local_roles),
                *caller_claims,
            ],
            database_config_name=self.name,
            translate_only=[
                claim for r, claim in role_claims.items() if r not in local_roles
            ],
            test_only=connection_test_only,
            register_claims=register_claims,
        )
        return engine.execution_options(**{
            SCHEMA_TRANSLATE_MAP_KEY: translate_map,
            EXECUTION_OPTION_DATABASE_CONFIG_NAME: self.name,
            EXECUTION_OPTION_TEST_ONLY: connection_test_only,
        })

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

    def _verify_route_for_schema_tag(self, vocab: Any, primary: Any) -> None:
        """ Verifies that vocab and primary are the same type, or both are Bindable. 

        Raises
        ------
        TypeError
            If vocab and primary are not the same type, and aren't both
            members of Bindable.
        """
        if (
            not (isinstance(vocab, Bindable) and isinstance(primary, Bindable))
            and type(vocab) is not type(primary)
        ):
            raise TypeError(
                f"vocab ({type(vocab).__name__}) and primary ({type(primary).__name__}) "
                "must be the same type."
            )

    def route_for_schema_tag(
        self,
        schema_tag: Role | str, 
        *,
        vocab: _T,
        primary: _T
    ) -> _T:
        """Determines the right connection, engine, or session to use 
        for a given schema_tag in a split-connection database.

        Notes
        -----
        Only Role.PRIMARY and any string are valid here, since ResolvedDatabase
        has no vocab/results connection-splitting.

        Raises
        ------
        TypeError
            If vocab and primary are not the same type, and aren't both
            members of Bindable.
        ValueError
            If schema_tag is a Role other than Role.PRIMARY, which has no
            meaning for this database.
        """
        self._verify_route_for_schema_tag(vocab=vocab, primary=primary)
        if isinstance(schema_tag, Role) and schema_tag != Role.PRIMARY:
            raise ValueError(
                f"Role.{schema_tag.name} has no meaning for {type(self).__name__}; "
                "only ResolvedCDMDatabase has vocab/results roles."
            )
        return primary

    def connection_for_schema_tag(self, schema_tag: Role | str) -> ResolvedConnection:
        """Return the connection hosting *schema_tag*'s physical schema.

        Mirrors :meth:`route_for_schema_tag`'s routing rule, so a caller
        asking where a tag lives gets the same answer as a caller routing a
        statement to it.
        """
        return self.route_for_schema_tag(
            schema_tag, vocab=self.connection, primary=self.connection
        )

    def foreign_key_can_span(self, from_tag: Role | str, to_tag: Role | str) -> bool:
        """Can a foreign key reference *to_tag*'s tables from *from_tag*'s?

        False when the two tags live on different physical databases, where
        no dialect can express the constraint. Ask this instead of deriving
        a split flag, so the answer stays correct if a deployment later
        colocates the two or federates them behind one connection.
        """
        return self.tags_share_a_transaction(from_tag, to_tag)

    def tags_share_a_transaction(self, *schema_tags: Role | str) -> bool:
        """Can one transaction cover statements against all of *schema_tags*?

        True only when every tag resolves to the same physical database.
        Nothing provides atomicity across two engines, so a caller needing
        all-or-nothing behaviour must check this first. Vacuously true for
        no tags or one tag.
        """
        keys = {
            ("ephemeral", id(connection))
            if is_ephemeral_url(connection._engine_url)
            else ("persistent", connection.physical_key())
            for tag in schema_tags
            for connection in (self.connection_for_schema_tag(tag),)
        }
        return len(keys) <= 1

    @cached_property
    def _cached_default_probed_physical_schemas(self) -> dict[str, str | None]:
        return self._probed_physical_schemas()

    def _probed_physical_schemas(
        self,
        probe_options: _SchemaProbeOptions | None = None,
    ) -> dict[str, str | None]:
        """Resolve unset schemas using the caller's engine options."""
        probe_options = probe_options or _SchemaProbeOptions()
        configured = self.configured_internal_schema_translate_map()
        unset = sorted(
            tag for tag, schema in configured.items()
            if schema is None
            and supports_schemas(self.connection_for_schema_tag(tag).dialect_name)
        )
        if not unset:
            return configured

        resolved = dict(configured)
        defaults: dict[str, str | None] = {}
        for tag in unset:
            connection = self.connection_for_schema_tag(tag)
            key = connection.physical_key()
            if key not in defaults:
                defaults[key] = self._probe_connection_default(connection, probe_options)
            resolved[tag] = defaults[key]
        return resolved

    @staticmethod
    def _probe_connection_default(
        connection: ResolvedConnection, options: _SchemaProbeOptions
    ) -> str | None:
        built_engine = options.built_engine
        use_built_engine = (
            built_engine is not None
            and options.built_connection is not None
            and connection.physical_key() == options.built_connection.physical_key()
        )
        engine = built_engine if use_built_engine else connection.create_engine(**options.engine_kwargs)
        if not use_built_engine:
            for extension in options.extensions:
                event.listens_for(engine, "connect")(extension)
        try:
            with engine.connect() as open_connection:
                return sa.inspect(open_connection).default_schema_name
        finally:
            if not use_built_engine:
                engine.dispose()

    def resolved_physical_schemas(
        self,
        *,
        probe_options: _SchemaProbeOptions | None = None,
    ) -> dict[str, str | None]:
        """Physical schema every internal tag resolves to, with unset ones
        filled in from their own connection's live default schema.

        A tag left unset in config means "use the connection's own default
        schema", which only that connection can answer. Reading it here, per
        tag, keeps one answer for the whole database, so engines built for
        different roles agree on where every tag lives.

        Stays ``None`` for a tag whose connection has no real schema concept,
        where ``None`` is the correct and final answer.

        Notes
        -----
        Opens one connection per distinct connection that has an unset tag,
        reusing the supplied engine for its own connection and forwarding the same
        engine options to any remote connection. No connection is opened when
        config names every schema.
        """
        if probe_options is None:
            return dict(self._cached_default_probed_physical_schemas)
        return self._probed_physical_schemas(probe_options)

    def schema_tags(self) -> tuple[Role, ...]:
        """Role tags whose schema provenance is worth tracking for this database."""
        return (Role.PRIMARY,)

    def roles_on_connection(self, connection: Connection | Engine) -> tuple[Role, ...]:
        """Role(s) this database entry has on *connection*'s own physical
        server (host, database, port), or ``()`` if none.

        *connection* may be an already-open ``Connection`` or an ``Engine``
        directly (``Engine.engine`` returns itself, so ``connection.engine.url``
        works either way).

        Only ``Role.PRIMARY`` is possible here, since ``ResolvedDatabase``
        has no vocab/results connection-splitting.
        """
        if self.connection.physical_key() == connection_key(connection.engine.url):
            return (Role.PRIMARY,)
        return ()

    def occupied_schemas(self, connection: sa.Connection) -> set[str]:
        """Physical schema names this database currently claims that live
        on *connection*'s own physical server.


        Parameters
        ----------
        connection : sqlalchemy.engine.Connection
            Open connection, used both to match this database's own roles
            against it and to read its live default schema
            (search_path-dependent, not a static per-dialect guess). The
            caller is expected to already have one in scope, not open a
            fresh one just for this.
        """
        roles = self.roles_on_connection(connection)
        if not roles:
            return set()
        default = sa.inspect(connection).default_schema_name
        translated = self.configured_internal_schema_translate_map()
        return {
            resolved
            for role in roles
            if (resolved := translated[role.value] or default) is not None
        }

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

    def route_for_schema_tag(
        self,
        schema_tag: Role | str, 
        *,
        vocab: _T,
        primary: _T
    ) -> _T:
        """Determines the right connection, engine, or session to use
        for a given schema_tag in a split-connection database.

        Role.VOCAB routes to *vocab*; Role.PRIMARY, Role.RESULTS, and any
        other string tag route to *primary* (Role.RESULTS has no
        connection of its own; it always resolves through Role.PRIMARY's).

        Raises
        ------
        TypeError
            If vocab and primary are not the same type, and aren't both
            members of Bindable.
        """
        self._verify_route_for_schema_tag(vocab=vocab, primary=primary)
        return vocab if schema_tag == Role.VOCAB else primary

    def connection_for_schema_tag(self, schema_tag: Role | str) -> ResolvedConnection:
        """Return the connection hosting *schema_tag*'s physical schema.

        ``Role.VOCAB`` resolves to ``vocab_connection``; every other tag
        resolves to the primary connection, matching
        :meth:`route_for_schema_tag`.
        """
        return self.route_for_schema_tag(
            schema_tag, vocab=self.vocab_connection, primary=self.connection
        )

    def schema_for_role(self, role: Role = Role.PRIMARY) -> str | None:
        """Return the effective schema for a given role.
        See :meth:`CDMDatabaseConfig.resolve` for how vocab/results roles are handled.

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

    def configured_internal_schema_translate_map(self) -> dict[str, str | None]:
        """SQLAlchemy schema translate map for OMOP ORM models.

        Routing:
          "primary" → cdm_schema
          "vocab"   → vocab_schema, falling back to cdm_schema if unset
          "results" → results_schema, falling back to cdm_schema if unset

        Notes
        -----
        This is NOT the full schema_translate_map for an engine!
        See :meth:`create_engine` for the full map, which merges the resolver's own 
        config-derived claims with any additional claims passed in by the caller.
        """
        return {
            Role.PRIMARY.value: schema_if_supported(self.schema_name, self.connection.dialect_name),
            Role.VOCAB.value: schema_if_supported(self.vocab_schema, self.vocab_connection.dialect_name),
            Role.RESULTS.value: schema_if_supported(self.results_schema, self.connection.dialect_name),
        }

    def schema_tags(self) -> tuple[Role, ...]:
        """Role tags whose schema provenance is worth tracking: primary, vocab, and results."""
        return (Role.PRIMARY, Role.VOCAB, Role.RESULTS)

    def roles_on_connection(self, connection: Connection | Engine) -> tuple[Role, ...]:
        """Role(s) this database entry has on *connection*'s own physical
        server (host, database, port), or ``()`` if none.

        ``Role.RESULTS`` is included whenever ``Role.PRIMARY`` is, since it
        has no connection of its own and always resolves through
        ``Role.PRIMARY``'s. When primary and vocab share one physical
        server, all three roles are returned.
        """
        target = connection_key(connection.engine.url)
        roles: list[Role] = []
        if self.connection.physical_key() == target:
            roles.extend((Role.PRIMARY, Role.RESULTS))
        if self.vocab_connection.physical_key() == target:
            roles.append(Role.VOCAB)
        return tuple(roles)

    def occupied_schemas(self, connection: sa.Connection) -> set[str]:
        """Physical schema names this database currently claims that live
        on *connection*'s own physical server.

        Scoped by :meth:`roles_on_connection`, so a schema belonging to a
        role hosted on a genuinely different physical server (e.g. vocab
        on its own connection) is never reported just because its name
        happens to coincide. Unlike checking every role unconditionally,
        this never needs to open a second engine to a different server.
        """
        roles = self.roles_on_connection(connection)
        if not roles:
            return set()
        default = sa.inspect(connection).default_schema_name
        translated = self.configured_internal_schema_translate_map()
        return {
            resolved
            for role in roles
            if (resolved := translated[role.value] or default) is not None
        }


    def create_engine(self, **kwargs: Any) -> Engine:
        """Always raises: a CDM database has a primary and a vocabulary engine.

        Raises
        ------
        TypeError
            Always. Use :meth:`create_engines` instead.
        """
        raise TypeError(
            f"{type(self).__name__} has a primary and a vocabulary engine. "
            "Use create_engines(), which returns (primary, vocab), so vocabulary "
            "work cannot silently land on the primary database."
        )

    def create_engines(
        self,
        *,
        schema_claims: Iterable[SchemaClaim] = (),
        extensions: Sequence[Callable[[Any, Any], None]] = (),
        execution_options: dict[str, Any] | None = None,
        owner: str | None = None,
        register_claims: bool = True,
        **kwargs: Any,
    ) -> tuple[Engine, Engine]:
        """Return ``(primary_engine, vocab_engine)``, the only way to get CDM engines.

        The vocabulary engine is the primary engine itself when
        ``vocab_connection`` addresses the same physical database, so a
        colocated deployment opens one pool. Every parameter applies to both
        engines and is as for :meth:`_build_engine`.
        """
        options: dict[str, Any] = dict(
            schema_claims=tuple(schema_claims),
            extensions=extensions,
            execution_options=execution_options,
            owner=owner if owner is not None else _derive_owner(),
            register_claims=register_claims,
            **kwargs,
        )
        primary = self._build_engine(Role.PRIMARY, **options)
        if (
            self.connection.name == self.vocab_connection.name
            or self.connection.addresses_same_database_as(self.vocab_connection)
        ):
            return primary, primary
        return primary, self._build_engine(Role.VOCAB, **options)

    def __repr__(self) -> str:
        return (
            f"ResolvedCDMDatabase(name={self.name!r}, "
            f"connection={self.connection.name!r}, "
            f"schema_name={self.schema_name!r}, "
            f"vocab_schema={self.vocab_schema!r}, "
            f"results_schema={self.results_schema!r})"
        )


def guard_schema_provenance_for(
    connection: sa.Connection,
    *,
    schema_tag: Role | str,
) -> AbstractContextManager[None]:
    """The schema-provenance guard scoped to schema_tag's own physical schema.

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Connection the guarded DDL runs on. Must have been built by
        create_engine(): the entry name and test_only are read back off its
        own execution_options, set there alongside schema_translate_map, so
        no separate `resolved` argument is needed.
    schema_tag : Role or str
        Schema tag being guarded.

    Returns
    -------
    AbstractContextManager[None]

    Raises
    ------
    ValueError
        If connection wasn't built by create_engine() (its execution_options
        carry neither the entry name nor test_only).
    """
    options = connection.get_execution_options()
    database_config_name = options.get(EXECUTION_OPTION_DATABASE_CONFIG_NAME)
    test_only = options.get(EXECUTION_OPTION_TEST_ONLY)
    if database_config_name is None or test_only is None:
        raise ValueError(
            "guard_schema_provenance_for() requires a connection built by create_engine(): "
            f"missing {EXECUTION_OPTION_DATABASE_CONFIG_NAME!r}/{EXECUTION_OPTION_TEST_ONLY!r} "
            "execution options."
        )
    schema_tag = schema_tag.value if isinstance(schema_tag, Role) else schema_tag
    return _guard_schema_provenance(
        connection,
        database_config_name=database_config_name,
        test_only=test_only,
        schema_tag=schema_tag,
        physical_schema=physical_schema_of(connection, schema_tag=schema_tag),
    )
