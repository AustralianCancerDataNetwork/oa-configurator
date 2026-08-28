"""Schema-aware SQL primitives.

``schema_translate_map`` (built in :mod:`schema`) only translates
Table/Sequence/Enum-derived Core constructs, and never raw ``text()``/
``table()`` or Inspector calls. The primitives below close this gap for the
few places where raw SQL is unavoidable.
"""

from __future__ import annotations

from types import EllipsisType
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, Inspector
from sqlalchemy.engine.interfaces import ReflectedColumn, ReflectedIndex
from sqlalchemy.orm import Session

Bindable = Engine | Connection | Session


def _as_bind(bindable: Bindable) -> Engine | Connection:
    """Reduce *bindable* to an ``Engine``/``Connection``.

    Parameters
    ----------
    bindable : sqlalchemy.engine.Engine or sqlalchemy.engine.Connection or sqlalchemy.orm.Session
        A bare ``Session`` has neither ``.dialect`` nor
        ``.get_execution_options()``, so it is reduced to its bound
        ``Engine`` first. ``Engine`` and ``Connection`` are interchangeable
        past this point for anything read-only, and are returned unchanged.

    Returns
    -------
    sqlalchemy.engine.Engine or sqlalchemy.engine.Connection
    """
    if isinstance(bindable, Session):
        return bindable.get_bind()
    return bindable


def schema_of(bindable: Bindable) -> str | None:
    """Return *bindable*'s default schema.

    Parameters
    ----------
    bindable : sqlalchemy.engine.Engine or sqlalchemy.engine.Connection or sqlalchemy.orm.Session
        Reduced via :func:`_as_bind` before reading its execution options.

    Returns
    -------
    str or None
        The ``None``-keyed entry of *bindable*'s ``schema_translate_map``,
        or ``None`` when no translate map is set at all.
    """
    bind = _as_bind(bindable)
    stm = bind.get_execution_options().get("schema_translate_map")
    return stm.get(None) if stm else None


def qualified(
    bindable: Bindable, name: str, *, schema: str | None | EllipsisType = ...
) -> str:
    """Quoted, schema-qualified identifier for raw SQL that's genuinely
    unavoidable.

    Parameters
    ----------
    bindable : sqlalchemy.engine.Engine or sqlalchemy.engine.Connection or sqlalchemy.orm.Session
        Supplies both the dialect (for identifier quoting) and, unless
        *schema* is given, the default schema.
    name : str
        Unqualified identifier to quote.
    schema : str, None, or ..., optional
        Three distinct states:
        1. omitted (the default, ``...``) infers the schema from  ``schema_of(bindable)``;
        2. ``None`` explicitly forces an unqualified name;
        3. any other string overrides the inferred schema with that exact name.

    Returns
    -------
    str
        The quoted identifier, schema-prefixed unless the effective schema
        is ``None``.
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


def ensure_schema(bindable: Engine | Connection, schema: str | None) -> None:
    """``CREATE SCHEMA IF NOT EXISTS``, dialect-aware.

    Parameters
    ----------
    bindable : sqlalchemy.engine.Engine or sqlalchemy.engine.Connection
        Given a ``Connection``, the DDL executes directly on it without
        opening a nested transaction, so it correctly participates in a
        caller's already-open transaction. Given an ``Engine``, it opens its
        own short-lived transaction instead.
    schema : str or None
        Schema to create. No-ops on SQLite (no schema concept) or when
        *schema* is ``None`` or ``"public"`` (always exists)

    Notes
    -----
    Unlike the schema primitives above, this parameter is required, not inferred,
    since creating the wrong schema by silent inference would be far worse
    than a missing default.
    """
    if schema is None or schema == "public":
        return
    bind = _as_bind(bindable)
    if bind.dialect.name == "sqlite":
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
    """Raise if *db_schema* collides with a schema reserved for internal bookkeeping.

    Parameters
    ----------
    db_schema : str or None
        Schema name to check.

    Raises
    ------
    RuntimeError
        If *db_schema* is reserved, naming the owning package.
    """
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
