"""Schema-aware SQL primitives.

schema_translate_map (built in schema.py) only translates Table/Sequence/
Enum-derived Core constructs, never raw text()/table() or Inspector calls.
These primitives close that gap.

Base layer: schema.py imports from here, not the other way around, so
Role lives here rather than in schema.py.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session
from sqlalchemy.sql.compiler import IdentifierPreparer

Bindable = Engine | Connection | Session

# SQLAlchemy's own execution_options key for schema translation
SCHEMA_TRANSLATE_MAP_KEY = "schema_translate_map"


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


def connection_key(url: sa.URL) -> str:
    """Physical identity of the database *url* addresses: host, port, and database name.

    Dialect, driver, and credentials are ignored, so the same database reached
    through another driver or as another user yields the same key.

    Parameters
    ----------
    url : sqlalchemy.URL

    Returns
    -------
    str
        ``"<host>:<port>/<database>"``, empty parts left blank.
    """
    return f"{url.host or ''}:{url.port or ''}/{url.database or ''}"


def is_ephemeral_url(safe_url: str) -> bool:
    """True if *safe_url* names a database that cannot be shared across
    independently-built engines.

    Only SQLite ``:memory:`` (plain or shared-cache ``mode=memory`` URI)
    databases are ephemeral this way: a second engine pointed at the same
    URL gets its own, disconnected, empty database rather than reconnecting
    to the first one's data. A caller that needs two engines to see the
    same ephemeral database must instead share one already-built engine or
    connection between them.
    """
    lowered = safe_url.lower()
    if not lowered.startswith("sqlite"):
        return False
    _, _, target = lowered.partition("://")
    target = target.lstrip("/")
    return target in ("", ":memory:") or "mode=memory" in lowered


def declared_schema_tags(tables: Iterable[sa.Table]) -> set[str]:
    """Every schema tag tables declare (Table.schema), skipping untagged tables.

    Parameters
    ----------
    tables : Iterable[sqlalchemy.Table]
        Tables to scan, e.g. ``metadata.tables.values()`` or any subset of it.

    Returns
    -------
    set[str]
        Every distinct non-None Table.schema value among tables.
    """
    return {table.schema for table in tables if table.schema is not None}


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

