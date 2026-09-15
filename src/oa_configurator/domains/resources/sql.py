"""Schema-aware SQL primitives.

schema_translate_map (built in schema.py) only translates Table/Sequence/
Enum-derived Core constructs, never raw text()/table() or Inspector calls.
These primitives close that gap.

Base layer: schema.py imports from here, not the other way around, so
Role lives here rather than in schema.py.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from types import EllipsisType
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, Inspector
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from .schema import ResolvedConnection, ResolvedDatabase

logger = logging.getLogger(__name__)

Bindable = Engine | Connection | Session


class Role(StrEnum):
    """Which physical target a database's logical role maps to.

    Every ResolvedDatabase picks a connection (.connection_target) and a
    schema (.schema_for_role) for a role.
    """

    PRIMARY = "primary"
    VOCAB = "vocab"
    RESULTS = "results"


class Dialect(StrEnum):
    """SQLAlchemy backend names (``Engine.dialect.name`` / ``get_backend_name()``)
    this codebase recognizes, independent of the driver.

    Distinct from ``ConnectionConfig.dialect``, which stays a free-form
    string to carry a driver suffix such as ``"postgresql+psycopg"``.
    """

    POSTGRESQL = "postgresql"
    SQLITE = "sqlite"


SCHEMA_TRANSLATE_MAP_KEY = "schema_translate_map"
"""The execution_options key SQLAlchemy itself defines for schema translation."""


@dataclass(frozen=True)
class DialectProfile:
    """Per-dialect facts this codebase needs, none of which vary by behavior.

    A plain data registry rather than a per-dialect class hierarchy: nothing
    here is a method that differs in *logic* per dialect (that's what
    OMOP_Alchemy's ``Backend`` split is for), it's three static facts looked
    up by dialect name.

    Attributes
    ----------
    default_schema : str | None
        This dialect's own default/unqualified schema name (e.g. Postgres's
        ``"public"``), or ``None`` if the dialect has no such concept.
    system_schemas : frozenset[str]
        Schema names this dialect reserves for its own internal catalogs.
    supports_schemas : bool
        Whether the dialect has a genuine multi-schema concept at all.
    """

    default_schema: str | None
    system_schemas: frozenset[str]
    supports_schemas: bool


_DIALECT_PROFILES: dict[str, DialectProfile] = {
    Dialect.POSTGRESQL: DialectProfile(
        default_schema="public",
        system_schemas=frozenset({"information_schema", "pg_catalog", "pg_toast"}),
        supports_schemas=True,
    ),
    Dialect.SQLITE: DialectProfile(
        default_schema=None,
        system_schemas=frozenset(),
        supports_schemas=False,
    ),
}

def _profile_for(dialect_name: str) -> DialectProfile:
    """DialectProfile for dialect_name.

    Raises
    ------
    ValueError
        If dialect_name isn't registered in _DIALECT_PROFILES. Deliberately
        not a silent fallback: this codebase only supports the dialects it
        actually models, and guessing schema behavior for one it doesn't
        would be worse than failing loudly at the point of use.
    """
    try:
        return _DIALECT_PROFILES[dialect_name]
    except KeyError:
        raise ValueError(
            f"Unsupported dialect {dialect_name!r}. Supported: "
            f"{sorted(str(d) for d in _DIALECT_PROFILES)}."
        ) from None

def _as_bind(bindable: Bindable) -> Engine | Connection:
    """Reduce bindable to an Engine/Connection.

    A bare Session has neither .dialect nor .get_execution_options(), so
    it's reduced to its bound Engine first.
    """
    if isinstance(bindable, Session):
        return bindable.get_bind()
    return bindable


def schema_of(bindable: Bindable, *, role: Role = Role.PRIMARY) -> str | None:
    """role's entry of bindable's schema_translate_map, or None if unset.

    Parameters
    ----------
    bindable : Engine | Connection | Session
    role : Role, optional
        Which schema_translate_map key to read. Defaults to Role.PRIMARY,
        matching every current caller.
    """
    bind = _as_bind(bindable)
    stm = bind.get_execution_options().get(SCHEMA_TRANSLATE_MAP_KEY)
    return stm.get(role.value) if stm else None


def qualified(
    bindable: Bindable,
    name: str,
    *,
    schema: str | None | EllipsisType = ...,
    role: Role = Role.PRIMARY,
) -> str:
    """Quoted, schema-qualified identifier, for raw SQL that's genuinely unavoidable.

    Parameters
    ----------
    bindable : Engine | Connection | Session
        The SQLAlchemy object whose dialect and schema_translate_map are used
        to quote and qualify the name.
    name : str
        Unqualified identifier to quote.
    schema : str | None | EllipsisType, optional
        Three distinct states:
        1. omitted (the default, ``...``) infers the schema from  ``schema_of(bindable, role=role)``;
        2. ``None`` explicitly forces an unqualified name;
        3. any other string overrides the inferred schema with that exact name.
    role : Role, optional
        Which schema_translate_map key to infer from when *schema* is
        omitted. Defaults to Role.PRIMARY; ignored if *schema* is given.

    Returns
    -------
    str
        The quoted identifier, schema-prefixed unless the effective schema
        is ``None``, e.g. ``"myschema"."mytable"`` or ``"mytable"``.
    """
    bind = _as_bind(bindable)
    effective_schema = schema_of(bind, role=role) if schema is ... else schema
    preparer = bind.dialect.identifier_preparer
    quoted_name = preparer.quote(name)
    if effective_schema is None:
        return quoted_name
    return f"{preparer.quote(effective_schema)}.{quoted_name}"


def _takes_schema_param(func: Any) -> bool:
    """True if func has a 'schema' parameter, False for anything unintrospectable."""
    try:
        return "schema" in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


class SchemaBoundInspector:
    """``sa.inspect()`` wrapper defaulting ``schema=`` to the bound engine's
    own ``schema_translate_map``.

    Every ``Inspector`` method that takes a ``schema`` parameter (found via
    ``inspect.signature()`` at class-definition time, not a hand-maintained
    list) is wrapped below to default it to the bound schema when omitted.
    Every other method delegates straight to the underlying ``Inspector``.

    Parameters
    ----------
    inspector : sqlalchemy.engine.reflection.Inspector
        The real inspector every call delegates to.
    schema : str or None
        Default schema applied to every wrapped method whenever its own
        ``schema=`` argument is omitted.
    """

    def __init__(self, inspector: Inspector, schema: str | None) -> None:
        self._inspector = inspector
        self._schema = schema

    def _resolve(self, schema: str | None | EllipsisType) -> str | None:
        """
        Parameters
        ----------
        schema : str, None, or ..., optional
            Three distinct states:
            1. omitted (the default, ``...``) infers the schema from  ``schema_of(bindable)``;
            2. ``None`` explicitly forces an unqualified name;
            3. any other string overrides the inferred schema with that exact name.
        """
        return self._schema if schema is ... else schema

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inspector, name)
        if callable(attr) and _takes_schema_param(getattr(type(self._inspector), name, None)):
            raise AttributeError(
                f"{name!r} takes a schema parameter but has no schema-defaulting wrapper on "
                "SchemaBoundInspector, likely a dialect-specific Inspector subclass method "
                "invisible to the class-time auto-wrap. Access self._inspector directly with "
                "an explicit schema=, or extend the auto-wrap to cover it."
            )
        return attr


def _make_schema_wrapper(name: str) -> Any:
    def wrapper(
        self: SchemaBoundInspector, *args: Any, schema: str | None | EllipsisType = ..., **kwargs: Any
    ) -> Any:
        return getattr(self._inspector, name)(*args, schema=self._resolve(schema), **kwargs)

    wrapper.__name__ = name
    wrapper.__doc__ = (
        f"``Inspector.{name}``, with ``schema=`` defaulted to this wrapper's bound "
        "schema when omitted. See SchemaBoundInspector."
    )
    return wrapper


for _name in dir(Inspector):
    if not _name.startswith("_") and _takes_schema_param(getattr(Inspector, _name, None)):
        setattr(SchemaBoundInspector, _name, _make_schema_wrapper(_name))
del _name


def schema_inspect(
    bindable: Bindable,
    *,
    schema: str | None | EllipsisType = ...,
    role: Role = Role.PRIMARY,
) -> SchemaBoundInspector:
    """``sa.inspect(bindable)``, wrapped so every ``Inspector`` method taking
    a ``schema`` parameter defaults it to ``schema_of(bindable, role=role)``
    instead of silently reflecting the wrong schema.

    Parameters
    ----------
    bindable : sqlalchemy.engine.Engine or sqlalchemy.engine.Connection or sqlalchemy.orm.Session
        Passed to both ``sa.inspect()`` and, unless *schema* is given,
        :func:`schema_of`.
    schema : str, None, or ..., optional
        Three distinct states:
        1. omitted (the default, ``...``) infers the schema from  ``schema_of(bindable, role=role)``;
        2. ``None`` explicitly forces an unqualified name;
        3. any other string overrides the inferred schema with that exact name.
    role : Role, optional
        Which schema_translate_map key to infer from when *schema* is
        omitted. Defaults to Role.PRIMARY; ignored if *schema* is given.

    Returns
    -------
    SchemaBoundInspector
        Wrapper around the real ``Inspector`` that applies the schema default
        to the four methods above.
    """
    bind = _as_bind(bindable)
    effective_schema = schema_of(bind, role=role) if schema is ... else schema
    return SchemaBoundInspector(sa.inspect(bind), effective_schema)


def schema_options(
    bindable: Bindable,
    *,
    schema: str | None | EllipsisType = ...,
    role: Role = Role.PRIMARY,
) -> dict[str, Any]:
    """Build a per-statement ``execution_options=...`` override.

    Parameters
    ----------
    bindable : sqlalchemy.engine.Engine or sqlalchemy.engine.Connection or sqlalchemy.orm.Session
        Source of the inferred schema, unless *schema* is given.
    schema : str, None, or ..., optional
        Three distinct states:
        1. omitted (the default, ``...``) infers the schema from  ``bindable``'s own map at *role*'s key;
        2. ``None`` explicitly forces an unqualified name;
        3. any other string overrides the inferred schema with that exact name.
    role : Role, optional
        Which schema_translate_map key to read (when *schema* is omitted)
        and write. Defaults to Role.PRIMARY.

    Returns
    -------
    dict
        ``{SCHEMA_TRANSLATE_MAP_KEY: {...}}``, ready to pass as
        ``execution_options=...`` on a single statement/connection.
        Carries forward every other role's entry already on ``bindable``'s
        own map, overriding only *role*'s key.
        ``execution_options(schema_translate_map=...)`` replaces the whole
        map rather than merging it, so rebuilding from scratch here would
        silently drop those other keys for a caller on a role-split
        ``ResolvedCDMDatabase``.
    """
    bind = _as_bind(bindable)
    existing = bind.get_execution_options().get(SCHEMA_TRANSLATE_MAP_KEY) or {}
    effective_schema = existing.get(role.value) if schema is ... else schema
    return {SCHEMA_TRANSLATE_MAP_KEY: {**existing, role.value: effective_schema}}


def supports_schemas(bindable: Bindable | str) -> bool:
    """True if the dialect has a genuine multi-schema concept.

    Parameters
    ----------
    bindable : Engine | Connection | Session | str
        A live bindable, or a dialect name directly (e.g. "sqlite"),
        for a caller that only has a `ResolvedConnection`/URL in hand and
        doesn't need (or want) to build an engine just to ask this.

    Notes
    -----
    False for SQLite, as:
    - every table lives in one flat file-level namespace, and
    - it can't create an inline FK across a schema boundary even when
    schema_translate_map resolves both sides to the same connection.
    """
    dialect_name = bindable if isinstance(bindable, str) else _as_bind(bindable).dialect.name
    return _profile_for(dialect_name).supports_schemas


def ensure_schema(bindable: Engine | Connection, schema: str | None) -> None:
    """CREATE SCHEMA IF NOT EXISTS, for Postgres and SQLite; not validated
    for other dialects.

    Given a Connection, executes directly on it (no nested transaction),
    so it participates in a caller's already-open transaction. Given an
    Engine, opens its own short-lived transaction.

    No-ops on a dialect with no multi-schema concept (e.g. SQLite), when
    schema is None, or when schema is that dialect's own default schema
    (e.g. Postgres's "public").

    Notes
    -----
    Unlike the schema primitives above, this parameter is required, not inferred,
    since creating the wrong schema by silent inference would be far worse
    than a missing default.
    """
    if schema is None:
        return
    bind = _as_bind(bindable)
    profile = _profile_for(bind.dialect.name)
    if schema == profile.default_schema or not profile.supports_schemas:
        return
    ddl = sa.schema.CreateSchema(schema, if_not_exists=True)
    if isinstance(bind, Engine):
        with bind.begin() as conn:
            conn.execute(ddl)
    else:
        bind.execute(ddl)


_reserved_schemas: dict[str, str] = {}


def register_reserved_schema(name: str, *, owner: str) -> None:
    """Register *name* as a schema no db_schema config may ever collide with.

    Parameters
    ----------
    name : str
        Schema name to reserve.
    owner : str
        Package reserving it, used in the error message on a later
        collision. Called once at module import time by the owning
        package, so the reservation is always in effect by the time any
        caller could reach :func:`reject_reserved_schema`.

    Raises
    ------
    RuntimeError
        If *name* is already reserved by a different owner. Re-registering
        the same name by the same owner is a no-op.
    """
    existing_owner = _reserved_schemas.get(name)
    if existing_owner is not None and existing_owner != owner:
        raise RuntimeError(
            f"Schema {name!r} is already reserved by {existing_owner!r}; "
            f"cannot also reserve it for {owner!r}."
        )
    _reserved_schemas[name] = owner


def _reserved_schema_message(db_schema: str | None) -> str | None:
    """Message describing why db_schema collides with a reserved schema, or None if it doesn't."""
    owner = _reserved_schemas.get(db_schema)
    if owner is None:
        return None
    return f"db_schema cannot be {db_schema!r}: reserved for internal use by {owner!r}."


def reject_reserved_schema(db_schema: str | None) -> None:
    """Raise RuntimeError if db_schema collides with a reserved schema, naming the owner."""
    message = _reserved_schema_message(db_schema)
    if message is not None:
        raise RuntimeError(message)


@contextmanager
def autocommit_connection(bindable: Engine | Connection) -> Iterator[Connection]:
    """Yield a ``Connection`` in ``AUTOCOMMIT`` isolation mode.

    Parameters
    ----------
    bindable : sqlalchemy.engine.Engine or sqlalchemy.engine.Connection
        Given an ``Engine``, opens a new connection for the duration of the
        ``with`` block and closes it on exit. Given an already-open
        ``Connection``, yields it with the isolation level overridden,
        restored to what it was on exit rather than left mutated for the
        rest of the connection's life.

    Notes
    -----
    - ``Connection`` has no ``.connect()`` of its own, so the two bindable
      arg types need different handling rather than one blind call chain.
    - The connection must not already have an active transaction:
      SQLAlchemy refuses to change ``isolation_level`` once one has started.

    Yields
    ------
    sqlalchemy.engine.Connection
    """
    bind = _as_bind(bindable)
    if isinstance(bind, Engine):
        connection = bind.connect()
        try:
            connection.execution_options(isolation_level="AUTOCOMMIT")
            yield connection
        finally:
            connection.close()
        return

    previous_isolation_level = bind.get_isolation_level()
    bind.execution_options(isolation_level="AUTOCOMMIT")
    try:
        yield bind
    finally:
        bind.rollback()
        bind.execution_options(isolation_level=previous_isolation_level)


# ── Schema-provenance drift: detection, prevention, rectification ──────────────

SCHEMA_PROVENANCE_SCHEMA = "oa_configurator_provenance"

register_reserved_schema(SCHEMA_PROVENANCE_SCHEMA, owner="oa_configurator")


class SchemaDriftError(RuntimeError):
    """A database's physical schema no longer matches its recorded provenance.

    Raised by guard_schema_provenance() before any DDL runs. Resolve via
    the acknowledge-schema-migration CLI command once the change is
    confirmed deliberate.
    """



def _provenance_schema_for(bindable: Bindable) -> str | None:
    """SCHEMA_PROVENANCE_SCHEMA on a dialect with real schema support, else None."""
    bind = _as_bind(bindable)
    return SCHEMA_PROVENANCE_SCHEMA if supports_schemas(bind) else None


def find_table_in_other_schemas(
    bindable: Bindable, table_name: str, *, expected_schema: str | None
) -> tuple[str, ...]:
    """Schemas, other than expected_schema, that already have a table
    named table_name. Excludes the dialect's own system schemas.
    """
    bind = _as_bind(bindable)
    inspector = sa.inspect(bind)
    system_schemas = _profile_for(bind.dialect.name).system_schemas
    candidates = [
        schema
        for schema in inspector.get_schema_names()
        if schema != expected_schema and schema not in system_schemas
    ]
    return tuple(
        schema for schema in candidates if inspector.has_table(table_name, schema=schema)
    )


def _provenance_target(
    resolved: ResolvedDatabase, role: Role | str
) -> tuple[ResolvedConnection, str | None, str]:
    """Resolve (connection, schema, role-value) for a provenance guard/record call.

    A ``Role`` member goes through the normal resolution machinery
    (``connection_target``/``schema_for_role``), covering primary/vocab/results.
    A bare string is for a schema this database's Role enum has no slot for
    (e.g. an extension schema, a package's own reserved schema): it always
    lives on the primary connection, and the string itself IS the schema, meaning
    there is nothing to resolve as the caller already knows the physical name.

    Returns
    -------
    connection : ResolvedConnection
        The connection to use for the provenance bookkeeping table and any guarded DDL
        matching the provided role.
    schema : str or None
        The schema to guard against drift, or None if the dialect has no schema concept.
        For a bare string role, the string itself is used directly as the schema.
    role_value : str
        The string value of the role, either a Role member's value or the bare string.
        For a bare string role, this is the same as the schema returned above.
    """
    if isinstance(role, Role):
        return resolved.connection_target(role), resolved.schema_for_role(role), role.value
    return resolved.connection_target(Role.PRIMARY), role, role


def _schema_provenance_table(bookkeeping_schema: str | None) -> sa.Table:
    """The (unbound) schema-provenance bookkeeping table definition.

    Built fresh on each call rather than shared/cached.
    """
    metadata = sa.MetaData()
    return sa.Table(
        "schema_provenance",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("database_name", sa.String(128), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("connection_safe_url", sa.String(512), nullable=False),
        sa.Column("resolved_schema", sa.String(128), nullable=True),
        sa.Column("first_recorded_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("previous_schema", sa.String(128), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("last_verified_at", sa.DateTime, server_default=sa.func.now(), nullable=True),
        sa.UniqueConstraint(
            "database_name", "role", "connection_safe_url",
            name="uq_schema_provenance_database_role_connection",
        ),
        schema=bookkeeping_schema,
    )


@contextmanager
def guard_schema_provenance(
    connection: Connection,
    resolved: ResolvedDatabase | None,
    *,
    role: Role | str,
    shared_as: str | None = None,
) -> Iterator[None]:
    """Guard against creating tables under a schema that silently drifted
    from a previously-recorded one.

    Enter checks; the write (a new provenance row, or a bumped
    last_verified_at on an existing one) happens only on successful exit,
    never when the wrapped block raises::

        with guard_schema_provenance(connection, resolved, role=Role.VOCAB):
            Base.metadata.create_all(bind=connection, tables=vocab_tables, checkfirst=True)

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection/transaction the guarded DDL runs on. The
        provenance table is created on it the first time it's needed.
    resolved : ResolvedDatabase or None
        Supplies database_name and the role-appropriate schema/connection,
        derived internally rather than three independent strings, so a
        caller can't pass a mismatched trio. None short-circuits to a
        no-op, for a bare-engine caller with no resolved config behind it
        (e.g. a test or programmatic caller). When not None, a role whose
        connection_target(role).test_only is True also short-circuits to a
        no-op, since a test-only database is already documented as
        disposable and a test that legitimately reconfigures its schema
        between runs would otherwise trip constant false positives. A test
        that wants to exercise real drift detection against a connection
        that's test_only=true at the config level should build its own
        resolved with connection.test_only overridden to False, rather
        than relying on a separate override parameter here.
    role : Role or str
        A ``Role`` member resolves its connection/schema the normal way. 
        A bare string is for an additional package-specific schema that is 
        not covered by ``Role`` (e.g. an extension schema, etc.). It's guarded
        against the primary connection, and the string itself is used directly
        as the schema being guarded, with no further resolution.
    shared_as : str, optional
        Overrides the identity this provenance record is tracked under,
        replacing ``resolved.name``. Use this when the schema being
        guarded is a resource shared across multiple database entries on
        the same connection, such as a model registry, rather than owned
        by this one database entry. Every caller of that shared resource
        must pass the same value, so they're tracked as one identity
        instead of each looking like an unrelated, unexplained occupant
        of the same schema. None (default) uses ``resolved.name``, which
        is correct for the normal case where the schema really is this
        database's own.

    Raises
    ------
    SchemaDriftError
        A previously-recorded schema for this database/role/connection
        disagrees with the current one, or no record exists yet but the
        target schema already has tables.

    Notes
    -----
    Accepted limitation: a connection repointed to a brand-new, genuinely
    empty server is indistinguishable from real day-one setup.
    """
    role_value = role.value if isinstance(role, Role) else role
    if resolved is None:
        logger.debug("guard_schema_provenance(role=%s): no resolved, skipping.", role_value)
        yield
        return
    target_connection, schema_name, role_value = _provenance_target(resolved, role)
    if target_connection.test_only:
        logger.debug("guard_schema_provenance(role=%s): test_only, skipping.", role_value)
        yield
        return

    database_name = shared_as if shared_as is not None else resolved.name
    connection_safe_url = target_connection.safe_url

    bookkeeping_schema = _provenance_schema_for(connection)
    ensure_schema(connection, bookkeeping_schema)
    table = _schema_provenance_table(bookkeeping_schema)
    table.create(bind=connection, checkfirst=True)

    existing_row = connection.execute(
        sa.select(table.c.resolved_schema).where(
            table.c.database_name == database_name,
            table.c.role == role_value,
            table.c.connection_safe_url == connection_safe_url,
        )
    ).first()

    if existing_row is not None:
        stored_schema = existing_row.resolved_schema
        if stored_schema != schema_name:
            raise SchemaDriftError(
                f"Schema drift detected for database {database_name!r} (role {role_value!r}): "
                f"previously resolved to schema {stored_schema!r}, now resolves to "
                f"{schema_name!r}. Run `acknowledge-schema-migration` once this change is "
                "confirmed deliberate."
            )
    else:
        already_populated = bool(
            schema_inspect(connection, schema=schema_name).get_table_names()
        )
        if already_populated:
            raise SchemaDriftError(
                f"Schema {schema_name!r} for database {database_name!r} (role {role_value!r}) "
                "already has tables, but no schema-provenance record exists for it. Run "
                "`acknowledge-schema-migration` to establish a baseline before proceeding."
            )

    yield

    if existing_row is not None:
        connection.execute(
            table.update()
            .where(
                table.c.database_name == database_name,
                table.c.role == role_value,
                table.c.connection_safe_url == connection_safe_url,
            )
            .values(last_verified_at=sa.func.now())
        )
    else:
        connection.execute(
            table.insert().values(
                database_name=database_name,
                role=role_value,
                connection_safe_url=connection_safe_url,
                resolved_schema=schema_name,
            )
        )


def record_schema_provenance(
    connection: Connection,
    resolved: ResolvedDatabase,
    *,
    role: Role | str,
    new_schema: str | None,
    reason: str,
    shared_as: str | None = None,
) -> None:
    """Overwrite the provenance baseline for resolved's database/role with new_schema.

    Makes new_schema the value guard_schema_provenance() treats as current
    from now on. Overwrites any existing row for this database/role: its
    prior resolved_schema moves into previous_schema rather than being
    discarded, but stops being treated as current, so callers must be sure
    new_schema is correct. Writes only this bookkeeping row; the CDM tables
    at either schema are never moved or dropped here (see
    drop_orphan_schema_tables for that).

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection/transaction the provenance update runs on. The
        provenance table is created on it the first time it's needed.
    resolved : ResolvedDatabase
        Supplies database_name and the role-appropriate schema/connection,
        derived internally rather than three independent strings, so a
        caller can't pass a mismatched trio.
    role : Role or str
        Schema role to record a new baseline for. A bare string is for a
        schema this database's ``Role`` enum has no slot for 
        (see ``guard_schema_provenance``'s ``role`` parameter for the same
        distinction).
    new_schema : str or None
        The new schema to record as the baseline for this database/role.
        None is allowed for a dialect that has no schema concept (e.g. SQLite).
    reason : str
        Human-readable explanation of why this schema change is deliberate.
    shared_as : str, optional
        See ``guard_schema_provenance``'s ``shared_as`` parameter: overrides
        the identity this record is tracked under, for a schema shared
        across multiple database entries rather than owned by this one.

    Raises
    ------
    ValueError
        If reason is blank.
    """
    if not reason.strip():
        raise ValueError("reason must not be blank.")

    database_name = shared_as if shared_as is not None else resolved.name
    target_connection, _, role_value = _provenance_target(resolved, role)
    connection_safe_url = target_connection.safe_url

    bookkeeping_schema = _provenance_schema_for(connection)
    ensure_schema(connection, bookkeeping_schema)
    table = _schema_provenance_table(bookkeeping_schema)
    table.create(bind=connection, checkfirst=True)

    existing_row = connection.execute(
        sa.select(table.c.resolved_schema).where(
            table.c.database_name == database_name,
            table.c.role == role_value,
            table.c.connection_safe_url == connection_safe_url,
        )
    ).first()

    if existing_row is not None:
        connection.execute(
            table.update()
            .where(
                table.c.database_name == database_name,
                table.c.role == role_value,
                table.c.connection_safe_url == connection_safe_url,
            )
            .values(
                previous_schema=existing_row.resolved_schema,
                resolved_schema=new_schema,
                acknowledged_at=sa.func.now(),
                reason=reason,
                last_verified_at=sa.func.now(),
            )
        )
    else:
        connection.execute(
            table.insert().values(
                database_name=database_name,
                role=role_value,
                connection_safe_url=connection_safe_url,
                resolved_schema=new_schema,
                previous_schema=None,
                acknowledged_at=sa.func.now(),
                reason=reason,
                last_verified_at=sa.func.now(),
            )
        )
