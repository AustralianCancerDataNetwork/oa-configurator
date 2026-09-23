"""Schema-aware SQL primitives.

schema_translate_map (built in schema.py) only translates Table/Sequence/
Enum-derived Core constructs, never raw text()/table() or Inspector calls.
These primitives close that gap.

Base layer: schema.py imports from here, not the other way around, so
Role lives here rather than in schema.py.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session
from sqlalchemy.sql.compiler import IdentifierPreparer

logger = logging.getLogger(__name__)

Bindable = Engine | Connection | Session


class Role(StrEnum):
    """Pre-determined logical schema tags for consumers.
    Each tag corresponds to a configurable schema name (depending
    on the database's type [Database vs. CDMDatabase]):
    - Role.PRIMARY -> schema or cdm_schema: primary tables
    - Role.VOCAB -> vocab_schema: vocabulary tables
    - Role.RESULTS -> results_schema: results tables

    Also disambiguates which physical connection to use on a CDMDatabase:
    Role.PRIMARY and Role.VOCAB each select a connection
    (connection_for_role, create_engine(role=...)); Role.RESULTS has no
    connection of its own and always resolves through Role.PRIMARY's.
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
    OMOP_Alchemy's ``Backend`` split is for), it's static facts looked up by
    dialect name.

    Attributes
    ----------
    system_schemas : frozenset[str]
        Schema names this dialect reserves for its own internal catalogs.
    supports_schemas : bool
        Whether the dialect has a genuine multi-schema concept at all.
    requires_host : bool
        Whether this dialect needs a real network host to connect (a
        server-based RDBMS), as opposed to a local file/embedded database
        (e.g. SQLite) that connects via a path instead. Defaults to True,
        since server-based dialects are the common case; a file-based
        dialect's profile overrides it explicitly.
    """

    system_schemas: frozenset[str]
    supports_schemas: bool
    requires_host: bool = True


_DIALECT_PROFILES: dict[str, DialectProfile] = {
    Dialect.POSTGRESQL: DialectProfile(
        system_schemas=frozenset({"information_schema", "pg_catalog", "pg_toast"}),
        supports_schemas=True,
    ),
    Dialect.SQLITE: DialectProfile(
        system_schemas=frozenset(),
        supports_schemas=False,
        requires_host=False,
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

    Notes
    -----
    On SQLite's SingletonThreadPool, Session.get_bind() can return
    the same underlying DBAPI connection the session already uses.
    If the inspector's wrapper around it is closed, it rolls back
    the session's own uncommitted work. 
    Solution: Return Session.connection() instead.
    """
    if isinstance(bindable, Session):
        return bindable.connection()
    return bindable


@contextmanager
def open_connection(bindable: Engine | Connection) -> Iterator[Connection]:
    """Opens its own transaction for an Engine, or uses
    an already-open Connection directly, participating in the caller's own
    transaction.

    Parameters
    ----------
    bindable : sqlalchemy.engine.Engine or sqlalchemy.engine.Connection
        An Engine opens a new connection and transaction scoped to this
        context manager, committing on a clean exit. A Connection is
        forwarded as-is; its transaction is owned by the caller, and
        passing the same Connection into several calls groups them into
        one shared transaction.

    Yields
    ------
    sqlalchemy.engine.Connection
    """
    if isinstance(bindable, Engine):
        with bindable.begin() as connection:
            yield connection
    else:
        yield bindable


def validate_schema_tag(table: sa.Table) -> str | None:
    """Whether the schema tag of a table is a known Role tag,
    a registered schema tag, or None if untagged.

    Raises
    ------
    ValueError
        If the schema is neither a Role value nor a registered schema tag.
    """
    schema = table.schema
    if schema is None:
        return None
    if schema in {member.value for member in Role} or schema in _RESERVED_SCHEMA_TAGS:
        return schema
    raise ValueError(f"{table} has unrecognized schema tag {schema!r}: not a Role and not registered.")


def physical_schema_of(bindable: Bindable, *, schema_tag: str | None = Role.PRIMARY) -> str | None:
    """Look up schema_tag's physical schema in the bindable's schema_translate_map,
    or return schema_tag unchanged if it has no entry there.

    Parameters
    ----------
    bindable : Engine | Connection | Session
    schema_tag : str or None, optional
        The schema_translate_map key to look up: a Role value or a
        registered schema tag, the same value a table's own schema
        attribute would carry. Defaults to Role.PRIMARY.

    Returns
    -------
    str or None
        - None if schema_tag is None
        - Physical schema if schema_tag is in the schema_translate_map
        - schema_tag itself if it has no entry in the schema_translate_map
    """
    if schema_tag is None:
        return None
    bind = _as_bind(bindable)
    stm = bind.get_execution_options().get(SCHEMA_TRANSLATE_MAP_KEY)
    return stm.get(schema_tag, schema_tag) if stm else schema_tag


def qualified(
    bindable: Bindable | IdentifierPreparer,
    name: str,
    *,
    physical_schema: str | None
) -> str:
    """Quoted, schema-qualified identifier, for raw SQL that's genuinely unavoidable.
    Utilises IdentifierPreparer.format_table to quote the name according to the dialect's rules.
    Parameters
    ----------
    bindable : Engine | Connection | Session | IdentifierPreparer
        The SQLAlchemy object whose dialect is used to quote the name, or an
        already-resolved IdentifierPreparer directly, for a caller with no
        live bindable in hand (e.g. a dialect-only preparer built ahead of
        any connection).
    name : str
        Unqualified identifier to quote.
    physical_schema : str or None
        The already-resolved schema to prefix with, or None for an
        unqualified name.

    Returns
    -------
    str
        The quoted identifier, schema-prefixed unless *physical_schema* is
        ``None``, e.g. ``"myschema"."mytable"`` or ``"mytable"``.
    """
    preparer = (
        bindable if isinstance(bindable, IdentifierPreparer)
        else _as_bind(bindable).dialect.identifier_preparer
    )
    return preparer.format_table(sa.table(name, schema=physical_schema))


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


def schema_if_supported(physical_schema: str | None, bindable: Bindable | str) -> str | None:
    """physical_schema if bindable's dialect has a genuine multi-schema concept, else None."""
    return physical_schema if supports_schemas(bindable) else None


def requires_host(bindable: Bindable | str) -> bool:
    """True if the dialect needs a real network host to connect, rather than
    a local file/embedded database (e.g. SQLite).

    Parameters
    ----------
    bindable : Engine | Connection | Session | str
        A live bindable, or a bare dialect name directly (e.g. "sqlite").
    """
    dialect_name = bindable if isinstance(bindable, str) else _as_bind(bindable).dialect.name
    return _profile_for(dialect_name).requires_host


def ensure_schema(bindable: Engine | Connection, physical_schema: str | None) -> None:
    """CREATE SCHEMA IF NOT EXISTS, for Postgres and SQLite; not validated
    for other dialects.

    Given a Connection, executes directly on it (no nested transaction),
    so it participates in a caller's already-open transaction. Given an
    Engine, opens its own short-lived transaction.

    No-ops on a dialect with no multi-schema concept (e.g. SQLite), when
    physical_schema is None, or when physical_schema is that dialect's own
    default schema (e.g. Postgres's "public").

    Notes
    -----
    Unlike the schema primitives above, this parameter is required, not inferred,
    since creating the wrong schema by silent inference would be far worse
    than a missing default.
    """
    if physical_schema is None:
        return
    bind = _as_bind(bindable)
    if not _profile_for(bind.dialect.name).supports_schemas:
        return
    if physical_schema == sa.inspect(bind).default_schema_name:
        return
    ddl = sa.schema.CreateSchema(physical_schema, if_not_exists=True)
    if isinstance(bind, Engine):
        with bind.begin() as conn:
            conn.execute(ddl)
    else:
        bind.execute(ddl)


_RESERVED_SCHEMAS: dict[str, str] = {}


def register_reserved_schema(name: str, *, owner: str) -> None:
    """Register *name* as a physical schema no db_schema config may ever collide with.

    Parameters
    ----------
    name : str
        Physical schema name to reserve.
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
    existing_owner = _RESERVED_SCHEMAS.get(name)
    if existing_owner is not None and existing_owner != owner:
        raise RuntimeError(
            f"Schema {name!r} is already reserved by {existing_owner!r}; "
            f"cannot also reserve it for {owner!r}."
        )
    _RESERVED_SCHEMAS[name] = owner


_RESERVED_SCHEMA_TAGS: dict[str, str] = {}


def register_reserved_schema_tag(name: str, *, owner: str) -> None:
    """Register *name* as a schema_translate_map tag validate_schema_tag() accepts,
    beyond the built-in Role values.

    Distinct from :func:`register_reserved_schema`: this registers a tag (a
    schema_translate_map key, never itself a literal schema), not a physical
    schema name a config value may collide with.

    Parameters
    ----------
    name : str
        Schema tag to register.
    owner : str
        Package registering it, used in the error message on a later
        collision. Called once at module import time by the owning
        package, so the registration is always in effect by the time any
        caller could reach :func:`validate_schema_tag`.

    Raises
    ------
    RuntimeError
        If *name* is already registered by a different owner. Re-registering
        the same name by the same owner is a no-op.
    """
    existing_owner = _RESERVED_SCHEMA_TAGS.get(name)
    if existing_owner is not None and existing_owner != owner:
        raise RuntimeError(
            f"Schema tag {name!r} is already registered by {existing_owner!r}; "
            f"cannot also register it for {owner!r}."
        )
    _RESERVED_SCHEMA_TAGS[name] = owner


def _reserved_schema_message(physical_schema: str | None) -> str | None:
    """Message describing why physical_schema collides with a reserved schema, or None if it doesn't."""
    owner = _RESERVED_SCHEMAS.get(physical_schema)
    if owner is None:
        return None
    return f"db_schema cannot be {physical_schema!r}: reserved for internal use by {owner!r}."


def reject_reserved_schema(physical_schema: str | None) -> None:
    """Raise RuntimeError if physical_schema collides with a reserved schema, naming the owner."""
    message = _reserved_schema_message(physical_schema)
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

    if bind.in_transaction():
        raise InvalidRequestError(
            "autocommit_connection() was given a Connection that already has an "
            "active transaction. Isolation level can't change mid-transaction; "
            "pass a fresh Connection or roll back first."
        )
    previous_isolation_level = bind.get_execution_options().get("isolation_level")
    if previous_isolation_level is None:
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



def find_table_in_other_schemas(
    bindable: Bindable, table_name: str, *, physical_schema: str | None
) -> tuple[str, ...]:
    """Schemas, other than physical_schema, that already have a table
    named table_name. Excludes the dialect's own system schemas.
    """
    bind = _as_bind(bindable)
    inspector = sa.inspect(bind)
    system_schemas = _profile_for(bind.dialect.name).system_schemas
    candidates = [
        schema
        for schema in inspector.get_schema_names()
        if schema != physical_schema and schema not in system_schemas
    ]
    return tuple(
        schema for schema in candidates if inspector.has_table(table_name, schema=schema)
    )


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
        sa.Column("schema_tag", sa.String(32), nullable=False),
        sa.Column("connection_safe_url", sa.String(512), nullable=False),
        sa.Column("physical_schema", sa.String(128), nullable=True),
        sa.Column("first_recorded_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("previous_physical_schema", sa.String(128), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("last_verified_at", sa.DateTime, server_default=sa.func.now(), nullable=True),
        sa.UniqueConstraint(
            "database_name", "schema_tag", "connection_safe_url",
            name="uq_schema_provenance_database_schema_tag_connection",
        ),
        schema=bookkeeping_schema,
    )


@contextmanager
def guard_schema_provenance(
    connection: Connection,
    *,
    database_name: str,
    test_only: bool,
    schema_tag: str,
    physical_schema: str | None,
    tables: Iterable[sa.Table],
) -> Iterator[None]:
    """Guard against creating tables under a schema that silently drifted
    from a previously-recorded one, or that already has a same-named table
    living under a different, unexpected schema.

    Checks on enter, yields to the caller's DDL block, then records the current schema
    in the bookkeeping table. Write does not happen if the caller's DDL block raises::

        with guard_schema_provenance(
            connection, database_name=resolved.name, test_only=resolved.vocab_connection.test_only,
            schema_tag=Role.VOCAB, physical_schema=physical_schema_of(connection, schema_tag=Role.VOCAB),
            tables=vocab_tables,
        ):
            Base.metadata.create_all(bind=connection, tables=vocab_tables, checkfirst=True)

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection/transaction the guarded DDL runs on. The
        provenance table is created on it the first time it's needed.
    database_name : str
        Identity this provenance record is tracked under. Pass a shared
        identity (e.g. "model_registry") when the schema being guarded is
        a resource shared across multiple database entries on the same
        connection -> resource is tracked as a shared asset. 
    test_only : bool
        The resolved connection's own ConnectionConfig.test_only. True
        skips the check entirely, since a test-only connection's schema is
        expected to change between runs, leading to constant false positives.
    schema_tag : str
        The schema_translate_map key this record is tracked under. Used
        only as this row's bookkeeping label; carries no connection- or
        schema-resolution meaning here (see *physical_schema* below).
    physical_schema : str or None
        The schema to guard, already resolved by the caller, e.g. via
        ``physical_schema_of(connection, schema_tag=schema_tag)``.
    tables : Iterable[sqlalchemy.Table]
        The tables about to be created under *physical_schema*, on a
        first-time setup (no provenance record yet). Each is checked
        against every other schema on the connection, catching a table
        already living under a different physical schema than
        *schema_tag* is configured for. Example: pre-existing vocab tables
        sitting in "myvocab" while vocab_schema is configured as "vocab".
        Pass an empty iterable when the caller has no specific tables in
        view (e.g. a read-only provenance check), which skips this
        specific check, not the whole guard.

    Raises
    ------
    SchemaDriftError
        - A previously-recorded schema for this database/schema_tag/connection
        disagrees with the current one,
        - No record exists yet but the target schema already has tables, or 
        - One of *tables* already exists under a different schema.

    Notes
    -----
    Accepted limitation: a connection repointed to a brand-new, genuinely
    empty server is indistinguishable from real day-one setup.
    """
    if test_only:
        logger.debug("guard_schema_provenance(schema_tag=%s): test_only, skipping.", schema_tag)
        yield
        return

    connection_safe_url = connection.engine.url.render_as_string(hide_password=True)

    bookkeeping_schema = schema_if_supported(SCHEMA_PROVENANCE_SCHEMA, connection)
    # Check occupancy before creating the bookkeeping table
    bookkeeping_table_exists = sa.inspect(connection).has_table(
        "schema_provenance", schema=bookkeeping_schema
    )
    existing_row = None
    if bookkeeping_table_exists:
        table = _schema_provenance_table(bookkeeping_schema)
        existing_row = connection.execute(
            sa.select(table.c.physical_schema).where(
                table.c.database_name == database_name,
                table.c.schema_tag == schema_tag,
                table.c.connection_safe_url == connection_safe_url,
            )
        ).first()

    if existing_row is None:
        already_populated = bool(
            sa.inspect(connection).get_table_names(schema=physical_schema)
        )
        if already_populated:
            raise SchemaDriftError(
                f"Schema {physical_schema!r} for database {database_name!r} (schema_tag {schema_tag!r}) "
                "already has tables, but no schema-provenance record exists for it. Run "
                "`acknowledge-schema-migration` to establish a baseline before proceeding."
            )

        if supports_schemas(connection):
            default_schema = sa.inspect(connection).default_schema_name
            expected_schema = physical_schema if physical_schema is not None else default_schema
            for guarded_table in tables:
                other_schemas = find_table_in_other_schemas(
                    connection, guarded_table.name, physical_schema=expected_schema
                )
                if other_schemas:
                    raise SchemaDriftError(
                        f"Table {guarded_table.name!r} for database {database_name!r} "
                        f"(schema_tag {schema_tag!r}) already exists under schema(s) "
                        f"{other_schemas!r}, not the configured schema {physical_schema!r}. "
                        f"{schema_tag!r} is likely misconfigured against pre-existing data. "
                        "Verify the correct schema, then run `acknowledge-schema-migration` "
                        "once confirmed."
                    )

    ensure_schema(connection, bookkeeping_schema)
    table = _schema_provenance_table(bookkeeping_schema)
    table.create(bind=connection, checkfirst=True)

    if existing_row is not None:
        stored_schema = existing_row.physical_schema
        if stored_schema != physical_schema:
            raise SchemaDriftError(
                f"Schema drift detected for database {database_name!r} (schema_tag {schema_tag!r}): "
                f"previously resolved to schema {stored_schema!r}, now resolves to "
                f"{physical_schema!r}. Run `acknowledge-schema-migration` once this change is "
                "confirmed deliberate."
            )

    yield

    if existing_row is not None:
        connection.execute(
            table.update()
            .where(
                table.c.database_name == database_name,
                table.c.schema_tag == schema_tag,
                table.c.connection_safe_url == connection_safe_url,
            )
            .values(last_verified_at=sa.func.now())
        )
    else:
        connection.execute(
            table.insert().values(
                database_name=database_name,
                schema_tag=schema_tag,
                connection_safe_url=connection_safe_url,
                physical_schema=physical_schema,
            )
        )


def record_schema_provenance(
    connection: Connection,
    *,
    database_name: str,
    schema_tag: str,
    new_physical_schema: str | None,
    reason: str,
) -> None:
    """Overwrite the provenance baseline for database_name/schema_tag with new_physical_schema.

    Makes new_physical_schema the value guard_schema_provenance() treats as
    current from now on. Overwrites any existing row for this
    database/schema_tag. Its prior physical_schema moves into
    previous_physical_schema. 

    Only for bookeeping; the CDM tables at either schema are never moved 
    or dropped here (see drop_orphan_schema_tables for that).

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection/transaction the provenance update runs on. The
        provenance table is created on it the first time it's needed.
    database_name : str
        Identity this provenance record is tracked under; see
        ``guard_schema_provenance``'s ``database_name`` parameter.
    schema_tag : str
        The schema_translate_map key this record is tracked under; see
        ``guard_schema_provenance``'s ``schema_tag`` parameter.
    new_physical_schema : str or None
        The new schema to record as the baseline for this database/schema_tag.
        None is allowed for a dialect that has no schema concept (e.g. SQLite).
    reason : str
        Human-readable explanation of why this schema change is deliberate.

    Raises
    ------
    ValueError
        If reason is blank.
    """
    if not reason.strip():
        raise ValueError("reason must not be blank.")

    connection_safe_url = connection.engine.url.render_as_string(hide_password=True)

    bookkeeping_schema = schema_if_supported(SCHEMA_PROVENANCE_SCHEMA, connection)
    ensure_schema(connection, bookkeeping_schema)
    table = _schema_provenance_table(bookkeeping_schema)
    table.create(bind=connection, checkfirst=True)

    existing_row = connection.execute(
        sa.select(table.c.physical_schema).where(
            table.c.database_name == database_name,
            table.c.schema_tag == schema_tag,
            table.c.connection_safe_url == connection_safe_url,
        )
    ).first()

    if existing_row is not None:
        connection.execute(
            table.update()
            .where(
                table.c.database_name == database_name,
                table.c.schema_tag == schema_tag,
                table.c.connection_safe_url == connection_safe_url,
            )
            .values(
                previous_physical_schema=existing_row.physical_schema,
                physical_schema=new_physical_schema,
                acknowledged_at=sa.func.now(),
                reason=reason,
                last_verified_at=sa.func.now(),
            )
        )
    else:
        connection.execute(
            table.insert().values(
                database_name=database_name,
                schema_tag=schema_tag,
                connection_safe_url=connection_safe_url,
                physical_schema=new_physical_schema,
                previous_physical_schema=None,
                acknowledged_at=sa.func.now(),
                reason=reason,
                last_verified_at=sa.func.now(),
            )
        )


def find_schema_provenance_claim(
    connection: Connection,
    *,
    physical_schema: str,
    exclude_database_name: str | None = None,
) -> str | None:
    """database_name of the schema_provenance row currently recording
    physical_schema as its baseline on this connection, or None.

    Checks only the current physical_schema column, never
    previous_physical_schema: a schema migrated away from is exactly what
    drop_orphan_schema_tables exists to clean up, so it must never be
    reported as still claimed.

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Matched via its own connection_safe_url; only rows for this exact
        connection are considered.
    physical_schema : str
        Physical schema to check for a current claim.
    exclude_database_name : str, optional
        Ignore rows for this database_name, regardless of schema_tag. Pass
        the database being acknowledged: its own primary/vocab/results tags
        legitimately sharing one physical schema is not a collision with
        itself.

    Returns
    -------
    str or None
        The claiming row's database_name, or None if nothing else on this
        connection currently records physical_schema as current.
    """
    bookkeeping_schema = schema_if_supported(SCHEMA_PROVENANCE_SCHEMA, connection)
    if not sa.inspect(connection).has_table("schema_provenance", schema=bookkeeping_schema):
        return None
    table = _schema_provenance_table(bookkeeping_schema)
    connection_safe_url = connection.engine.url.render_as_string(hide_password=True)
    conditions = [
        table.c.connection_safe_url == connection_safe_url,
        table.c.physical_schema == physical_schema,
    ]
    if exclude_database_name is not None:
        conditions.append(table.c.database_name != exclude_database_name)
    row = connection.execute(sa.select(table.c.database_name).where(*conditions)).first()
    return row.database_name if row is not None else None
