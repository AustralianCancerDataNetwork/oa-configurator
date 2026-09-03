"""Schema-aware SQL primitives.

schema_translate_map (built in schema.py) only translates Table/Sequence/
Enum-derived Core constructs, never raw text()/table() or Inspector calls.
These primitives close that gap.

Base layer: schema.py imports from here, not the other way around, so
Role lives here rather than in schema.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from types import EllipsisType
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, Inspector
from sqlalchemy.engine.interfaces import ReflectedColumn, ReflectedIndex
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from .schema import ResolvedDatabase

Bindable = Engine | Connection | Session


class Role(StrEnum):
    """Which physical target a database's logical role maps to.

    Every ResolvedDatabase picks a connection (.connection_target) and a
    schema (.schema_for_role) for a role -- only PRIMARY is valid on the
    base class; ResolvedCDMDatabase's override adds VOCAB (its own
    connection and schema) and RESULTS (its own schema, but no connection
    of its own -- it shares PRIMARY's).
    """

    PRIMARY = "primary"
    VOCAB = "vocab"
    RESULTS = "results"


def _as_bind(bindable: Bindable) -> Engine | Connection:
    """Reduce bindable to an Engine/Connection.

    A bare Session has neither .dialect nor .get_execution_options(), so
    it's reduced to its bound Engine first.
    """
    if isinstance(bindable, Session):
        return bindable.get_bind()
    return bindable


def schema_of(bindable: Bindable) -> str | None:
    """The None-keyed entry of bindable's schema_translate_map, or None if unset."""
    bind = _as_bind(bindable)
    stm = bind.get_execution_options().get("schema_translate_map")
    return stm.get(None) if stm else None


def qualified(
    bindable: Bindable, name: str, *, schema: str | None | EllipsisType = ...
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
        1. omitted (the default, ``...``) infers the schema from  ``schema_of(bindable)``;
        2. ``None`` explicitly forces an unqualified name;
        3. any other string overrides the inferred schema with that exact name.
    
    Returns
    -------
    str
        The quoted identifier, schema-prefixed unless the effective schema
        is ``None``, e.g. ``"myschema"."mytable"`` or ``"mytable"``.
    """
    bind = _as_bind(bindable)
    effective_schema = schema_of(bind) if schema is ... else schema
    preparer = bind.dialect.identifier_preparer
    quoted_name = preparer.quote(name)
    if effective_schema is None:
        return quoted_name
    return f"{preparer.quote(effective_schema)}.{quoted_name}"


class SchemaBoundInspector:
    """``sa.inspect()`` wrapper defaulting ``schema=`` to the bound engine's
    own ``schema_translate_map``.

    Every method not listed below delegates straight to the underlying
    ``Inspector`` with no schema default applied.

    Parameters
    ----------
    inspector : sqlalchemy.engine.reflection.Inspector
        The real inspector every call delegates to.
    schema : str or None
        Default schema applied to ``has_table``/``get_columns``/
        ``get_indexes``/``get_table_names`` below whenever their own
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

    def has_table(
        self, table_name: str, *, schema: str | None | EllipsisType = ...
    ) -> bool:
        """
        Parameters
        ----------
        table_name : str
            Table to look up.
        schema : str, None, or ..., optional
            Per-call override of ``self._schema``. See :class:`SchemaBoundInspector`.

        Returns
        -------
        bool
            True if the table exists in the effective schema, False otherwise.
        """
        return self._inspector.has_table(table_name, schema=self._resolve(schema))

    def get_columns(
        self, table_name: str, *, schema: str | None | EllipsisType = ..., **kwargs: Any
    ) -> list[ReflectedColumn]:
        """
        Parameters
        ----------
        table_name : str
            Table to reflect.
        schema : str, None, or ..., optional
            Per-call override of ``self._schema``. See :class:`SchemaBoundInspector`.
        **kwargs
            Forwarded to ``Inspector.get_columns``.

        Returns
        -------
        list of sqlalchemy.engine.interfaces.ReflectedColumn
            Column metadata for the table in the effective schema.
        """
        return self._inspector.get_columns(
            table_name, schema=self._resolve(schema), **kwargs
        )

    def get_indexes(
        self, table_name: str, *, schema: str | None | EllipsisType = ..., **kwargs: Any
    ) -> list[ReflectedIndex]:
        """
        Parameters
        ----------
        table_name : str
            Table to reflect.
        schema : str, None, or ..., optional
            Per-call override of ``self._schema``. See :class:`SchemaBoundInspector`.
        **kwargs
            Forwarded to ``Inspector.get_indexes``.

        Returns
        -------
        list of sqlalchemy.engine.interfaces.ReflectedIndex
            Index metadata for the table in the effective schema.
        """
        return self._inspector.get_indexes(
            table_name, schema=self._resolve(schema), **kwargs
        )

    def get_table_names(
        self, *, schema: str | None | EllipsisType = ..., **kwargs: Any
    ) -> list[str]:
        """
        Parameters
        ----------
        schema : str, None, or ..., optional
            Per-call override of ``self._schema``. See :class:`SchemaBoundInspector`.
        **kwargs
            Forwarded to ``Inspector.get_table_names``.

        Returns
        -------
        list of str
            Table names in the effective schema.
        """
        return self._inspector.get_table_names(schema=self._resolve(schema), **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inspector, name)


def schema_inspect(
    bindable: Bindable, *, schema: str | None | EllipsisType = ...
) -> SchemaBoundInspector:
    """``sa.inspect(bindable)``, wrapped so ``has_table``/``get_columns``/
    ``get_indexes``/``get_table_names`` default ``schema=`` to
    ``schema_of(bindable)`` instead of silently reflecting the wrong schema.

    Parameters
    ----------
    bindable : sqlalchemy.engine.Engine or sqlalchemy.engine.Connection or sqlalchemy.orm.Session
        Passed to both ``sa.inspect()`` and, unless *schema* is given,
        :func:`schema_of`.
    schema : str, None, or ..., optional
        Three distinct states:
        1. omitted (the default, ``...``) infers the schema from  ``schema_of(bindable)``;
        2. ``None`` explicitly forces an unqualified name;
        3. any other string overrides the inferred schema with that exact name.

    Returns
    -------
    SchemaBoundInspector
        Wrapper around the real ``Inspector`` that applies the schema default
        to the four methods above.  
    """
    bind = _as_bind(bindable)
    effective_schema = schema_of(bind) if schema is ... else schema
    return SchemaBoundInspector(sa.inspect(bind), effective_schema)


def schema_options(
    bindable: Bindable, *, schema: str | None | EllipsisType = ...
) -> dict[str, Any]:
    """Build a per-statement ``execution_options=...`` override.

    Parameters
    ----------
    bindable : sqlalchemy.engine.Engine or sqlalchemy.engine.Connection or sqlalchemy.orm.Session
        Source of the inferred schema, unless *schema* is given.
    schema : str, None, or ..., optional
        Three distinct states:
        1. omitted (the default, ``...``) infers the schema from  ``schema_of(bindable)``;
        2. ``None`` explicitly forces an unqualified name;
        3. any other string overrides the inferred schema with that exact name.

    Returns
    -------
    dict
        ``{"schema_translate_map": {None: <effective schema>}}``, ready to
        pass as ``execution_options=...`` on a single statement/connection.
    """
    bind = _as_bind(bindable)
    effective_schema = schema_of(bind) if schema is ... else schema
    return {"schema_translate_map": {None: effective_schema}}


def supports_schemas(bindable: Bindable) -> bool:
    """True if bindable's dialect has a genuine multi-schema concept.

    Notes
    -----
    False for SQLite, as:
    - every table lives in one flat file-level namespace, and 
    - it can't create an inline FK across a schema boundary even when
    schema_translate_map resolves both sides to the same connection.
    """
    bind = _as_bind(bindable)
    return bind.dialect.name != "sqlite"


def ensure_schema(bindable: Engine | Connection, schema: str | None) -> None:
    """CREATE SCHEMA IF NOT EXISTS, dialect-aware.

    Given a Connection, executes directly on it (no nested transaction),
    so it participates in a caller's already-open transaction. Given an
    Engine, opens its own short-lived transaction.

    No-ops on SQLite, or when schema is None or "public". 
    Notes
    -----
    Unlike the schema primitives above, this parameter is required, not inferred,
    since creating the wrong schema by silent inference would be far worse
    than a missing default.
    """
    if schema is None or schema == "public":
        return
    bind = _as_bind(bindable)
    if not supports_schemas(bind):
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


def reject_reserved_schema(db_schema: str | None) -> None:
    """Raise RuntimeError if db_schema collides with a reserved schema, naming the owner."""
    owner = _reserved_schemas.get(db_schema)
    if owner is not None:
        raise RuntimeError(
            f"db_schema cannot be {db_schema!r}: reserved for internal use by {owner!r}."
        )


def autocommit_connection(bindable: Engine | Connection) -> Connection:
    """Return a ``Connection`` in ``AUTOCOMMIT`` isolation mode.

    Parameters
    ----------
    bindable : sqlalchemy.engine.Engine or sqlalchemy.engine.Connection
        Given an ``Engine``, opens and returns a new connection. Given an
        already-open ``Connection``, returns it with the isolation level
        overridden instead.

    Notes
    -----
    - ``Connection`` has no ``.connect()`` of its own, so the two bindable
      arg types need different handling rather than one blind call chain.
    - The connection must not already have an active transaction:
      SQLAlchemy refuses to change ``isolation_level`` once one has started.

    Returns
    -------
    sqlalchemy.engine.Connection
    """
    bind = _as_bind(bindable)
    if isinstance(bind, Engine):
        return bind.connect().execution_options(isolation_level="AUTOCOMMIT")
    return bind.execution_options(isolation_level="AUTOCOMMIT")


# ── Schema-provenance drift: detection, prevention, rectification ──────────────

SCHEMA_PROVENANCE_SCHEMA = "oa_configurator_provenance"

register_reserved_schema(SCHEMA_PROVENANCE_SCHEMA, owner="oa_configurator")


class SchemaDriftError(RuntimeError):
    """A database's physical schema no longer matches its recorded provenance.

    Raised by guard_schema_provenance() before any DDL runs. Resolve via
    the acknowledge-schema-migration CLI command once the change is
    confirmed deliberate.
    """


_SYSTEM_SCHEMAS: dict[str, frozenset[str]] = {
    "postgresql": frozenset({"information_schema", "pg_catalog", "pg_toast"}),
}


def _system_schemas_for(dialect_name: str) -> frozenset[str]:
    """Schema names dialect_name (e.g. "postgresql") reserves for its own
    internal catalogs. Empty for a dialect not listed here.
    """
    return _SYSTEM_SCHEMAS.get(dialect_name, frozenset())


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
    system_schemas = _system_schemas_for(bind.dialect.name)
    candidates = [
        schema
        for schema in inspector.get_schema_names()
        if schema != expected_schema and schema not in system_schemas
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
    resolved: ResolvedDatabase,
    *,
    role: Role,
    test_only: bool = False,
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
    resolved : ResolvedDatabase
        Supplies database_name and the role-appropriate schema/connection,
        derived internally rather than three independent strings, so a
        caller can't pass a mismatched trio.
    role : Role
        No default: every real call site already knows its role.
    test_only : bool, optional
        True short-circuits to a no-op, since a test_only database is already
        documented as disposable and a test that legitimately reconfigures
        its schema between runs would otherwise trip constant false
        positives.

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
    if test_only:
        yield
        return

    database_name = resolved.name
    role_value = str(role.value)
    schema_name = resolved.schema_for_role(role)
    connection_safe_url = resolved.connection_target(role).safe_url

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
    role: Role,
    new_schema: str | None,
    reason: str,
) -> None:
    """Overwrite the provenance baseline for resolved's database/role with new_schema.

    A deliberate write, not a passive acknowledgment: this is what makes
    new_schema the value guard_schema_provenance() treats as current from
    now on. If a row already exists, its resolved_schema moves into
    previous_schema and is replaced by new_schema -- callers must be sure
    new_schema is correct, since the row's prior value stops being treated
    as current the moment this returns.

    Covers both hard-stop cases guard_schema_provenance() raises on:
    1. a genuine schema migration (a row exists and disagrees), and
    2. retrofitting provenance onto an already-populated deployment
    (no row exists yet, but the target schema already has tables).

    Only this bookkeeping row is written. The actual CDM tables at either
    the old or new schema are never moved, dropped, or otherwise touched
    here -- reconciling the real data is the operator's own responsibility
    (see drop_orphan_schema_tables for the cleanup half of that).

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection/transaction the provenance update runs on. The
        provenance table is created on it the first time it's needed.
    resolved : ResolvedDatabase
        Supplies database_name and the role-appropriate schema/connection,
        derived internally rather than three independent strings, so a
        caller can't pass a mismatched trio.
    role : Role
        Schema role to record a new baseline for.
    new_schema : str or None
        The new schema to record as the baseline for this database/role.
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

    database_name = resolved.name
    role_value = str(role.value)
    connection_safe_url = resolved.connection_target(role).safe_url

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
