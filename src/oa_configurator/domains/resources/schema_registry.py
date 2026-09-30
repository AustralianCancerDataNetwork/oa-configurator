"""Schema registry: ownership, reservation, and drift detection.

Tracks which physical schema each database entry's schema_translate_map
tag currently resolves to, in a live connection-backed table
(``schema_registry``). Detects drift, ownership conflicts, and
reservation collisions.

``create_engine()`` in ``domains/resources/schema.py`` is the sole entry
point: every primitive in this module is private. The public surface is
``create_engine()``, ``guard_schema_provenance_for()``,
``physical_schema_of()``, and ``claimed_schema_tags()``.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .sql import (
    SCHEMA_TRANSLATE_MAP_KEY,
    Bindable,
    Role,
    _as_bind,
    _profile_for,
    ensure_schema,
    open_connection,
    schema_if_supported,
    supports_schemas,
)

logger = logging.getLogger(__name__)

# Registered as reserved in create_engine()
_SCHEMA_PROVENANCE_SCHEMA = "oa_configurator_provenance"


class SchemaDriftError(RuntimeError):
    """A database's physical schema no longer matches its recorded provenance.
    """


class SchemaOwnershipError(RuntimeError):
    """A schema tag or a reserved physical schema is already claimed by a
    different owner on the same connection.
    """


class UnregisteredSchemaTagError(RuntimeError):
    """A schema_tag fell through to a literal physical schema that was
    never registered on this connection.
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


class _SchemaRegistryBase(DeclarativeBase):
    """Dedicated declarative base for the schema-registry bookkeeping table."""

SCHEMA_REGISTRY_TABLE_NAME = "schema_registry"

class SchemaRegistry(_SchemaRegistryBase):
    """One schema-registry row: schema_tag's claimed, reserved, or drifted
    physical schema for one database entry on one connection.

    One row shape serves three checks: drift, ownership conflicts, and
    reservation conflicts, all scoped to one connection.

    Declared with a fixed schema (``_SCHEMA_PROVENANCE_SCHEMA``) as its
    structural definition only. Every function in this module queries a
    copy of this table bound to the connection's own resolved schema via
    :func:`_schema_registry_table`, never these columns directly.
    """

    __tablename__ = SCHEMA_REGISTRY_TABLE_NAME
    __table_args__ = (
        sa.UniqueConstraint(
            "database_name",
            "schema_tag",
            "connection_safe_url",
            name="uq_schema_registry_database_schema_tag_connection",
        ),
        sa.Index(
            "uq_schema_registry_reserved_connection_schema",
            "connection_safe_url",
            "physical_schema",
            unique=True,
            postgresql_where=sa.text("reserved IS TRUE"),
            sqlite_where=sa.text("reserved IS TRUE"),
        ),
        {"schema": _SCHEMA_PROVENANCE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    database_name: Mapped[str] = mapped_column(sa.String(128))
    schema_tag: Mapped[str] = mapped_column(sa.String(32))
    connection_safe_url: Mapped[str] = mapped_column(sa.String(512))
    physical_schema: Mapped[str | None] = mapped_column(sa.String(128))
    owner: Mapped[str | None] = mapped_column(sa.String(128))
    reserved: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    first_recorded_at: Mapped[datetime.datetime] = mapped_column(server_default=sa.func.now())
    previous_physical_schema: Mapped[str | None] = mapped_column(sa.String(128))
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column()
    reason: Mapped[str | None] = mapped_column(sa.Text)
    last_verified_at: Mapped[datetime.datetime | None] = mapped_column(server_default=sa.func.now())


def _schema_registry_table(schema: str | None) -> sa.Table:
    """A standalone Table for schema_registry, bound to *schema*.

    Parameters
    ----------
    schema : str or None
        Physical schema to bind the table to, resolved by the caller via
        ``schema_if_supported(_SCHEMA_PROVENANCE_SCHEMA, connection)``.
    """
    return cast(sa.Table, SchemaRegistry.__table__).to_metadata(sa.MetaData(), schema=schema)  # ty: ignore[invalid-argument-type]


def _connection_safe_url(connection: Connection) -> str:
    """This connection's own URL, credentials redacted; every row in this
    table is scoped by it."""
    return connection.engine.url.render_as_string(hide_password=True)


def _create_schema_registry_table(connection: Connection) -> sa.Table:
    """Create the schema_registry table on this connection if it doesn't
    exist yet, and return the Table bound to wherever it physically lives.
    """
    physical_schema = schema_if_supported(_SCHEMA_PROVENANCE_SCHEMA, connection)
    ensure_schema(connection, physical_schema)
    table = _schema_registry_table(physical_schema)
    table.create(bind=connection, checkfirst=True)
    return table

def _reject_ownership_conflict(
    connection: Connection,
    table: sa.Table,
    *,
    connection_safe_url: str,
    schema_tag: str,
    physical_schema: str | None,
    owner: str | None,
) -> None:
    """Raise SchemaOwnershipError if a different owner already claims schema_tag
    on this connection with a different physical_schema."""
    conflict = connection.execute(
        sa.select(table.c.owner, table.c.physical_schema).where(
            table.c.connection_safe_url == connection_safe_url,
            table.c.schema_tag == schema_tag,
            table.c.owner.is_not(None),
            table.c.owner != owner,
            table.c.physical_schema != physical_schema,
        )
    ).first()
    if conflict is not None:
        raise SchemaOwnershipError(
            f"Schema tag {schema_tag!r} on this connection is already claimed by "
            f"{conflict.owner!r} (physical schema {conflict.physical_schema!r}); "
            f"cannot also claim it for {owner!r} (physical schema {physical_schema!r})."
        )


def _reject_reservation_conflict(
    connection: Connection,
    table: sa.Table,
    *,
    connection_safe_url: str,
    physical_schema: str | None,
    owner: str | None,
) -> None:
    """Raise SchemaOwnershipError if a different owner already reserves
    physical_schema on this connection, regardless of schema_tag."""
    conflict = connection.execute(
        sa.select(table.c.owner, table.c.database_name).where(
            table.c.connection_safe_url == connection_safe_url,
            table.c.physical_schema == physical_schema,
            table.c.reserved.is_(True),
            table.c.owner.is_not(None),
            table.c.owner != owner,
        )
    ).first()
    if conflict is not None:
        raise SchemaOwnershipError(
            f"Physical schema {physical_schema!r} on this connection is already reserved "
            f"by {conflict.owner!r} (for database {conflict.database_name!r}); "
            f"{owner!r} cannot also use it."
        )


def _register_schema_claim(
    connection: Connection,
    *,
    database_name: str,
    schema_tag: str,
    physical_schema: str | None,
    owner: str | None = None,
    reserved: bool = False,
) -> None:
    """Claim schema_tag/physical_schema on this connection for owner, checked
    against every other claim sharing the connection.

    Called only from create_engine().

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection/transaction the claim is checked and recorded
        against. Ownership and reservation conflicts are scoped to this
        connection's own URL.
    database_name : str
        Identity this row's drift baseline is tracked under.
    schema_tag : str
        The schema_translate_map key being claimed. A reservation-only
        claim uses physical_schema as its own tag.
    physical_schema : str or None
        The physical schema schema_tag resolves to. None is a no-op.
    owner : str, optional
        The claiming package. None for the resolver's own Role entries.
    reserved : bool, optional
        True if physical_schema may not be used by any other owner on
        this connection, regardless of schema_tag.

    Raises
    ------
    SchemaOwnershipError
        If a different owner already claims schema_tag with a different
        physical_schema, or already reserves this physical_schema.
    SchemaDriftError
        On first registration, if physical_schema already has tables in
        it with no existing baseline. Run `acknowledge-schema-migration`
        to confirm the change deliberately.
    """
    if physical_schema is None:
        return

    connection_safe_url = _connection_safe_url(connection)
    # Snapshotted before the registry table is created, so registering
    # _SCHEMA_PROVENANCE_SCHEMA itself for the first time doesn't see its
    # own just-created table and mistake that for pre-existing data.
    already_populated = supports_schemas(connection) and bool(
        sa.inspect(connection).get_table_names(schema=physical_schema)
    )

    table = _create_schema_registry_table(connection)

    _reject_ownership_conflict(
        connection, table,
        connection_safe_url=connection_safe_url,
        schema_tag=schema_tag,
        physical_schema=physical_schema,
        owner=owner,
    )
    _reject_reservation_conflict(
        connection, table,
        connection_safe_url=connection_safe_url,
        physical_schema=physical_schema,
        owner=owner,
    )

    if reserved:
        row_conditions = [
            table.c.connection_safe_url == connection_safe_url,
            table.c.physical_schema == physical_schema,
            table.c.reserved.is_(True),
        ]
    else:
        row_conditions = [
            table.c.database_name == database_name,
            table.c.schema_tag == schema_tag,
            table.c.connection_safe_url == connection_safe_url,
        ]
    existing_row = connection.execute(
        sa.select(table.c.physical_schema).where(*row_conditions)
    ).first()

    if existing_row is None:
        if already_populated:
            raise SchemaDriftError(
                f"Schema {physical_schema!r} for database {database_name!r} (schema_tag "
                f"{schema_tag!r}) already has tables, but no schema-registry record exists "
                "for it. Run `acknowledge-schema-migration` to establish a baseline before "
                "proceeding."
            )
        connection.execute(
            sa.insert(table).values(
                database_name=database_name,
                schema_tag=schema_tag,
                connection_safe_url=connection_safe_url,
                physical_schema=physical_schema,
                owner=owner,
                reserved=reserved,
            )
        )
    elif existing_row.physical_schema == physical_schema:
        # Same claim re-asserted: refresh owner/last_verified_at. A
        # different physical_schema for the same key is drift, left for
        # _guard_schema_provenance() to catch, not resolved here.
        connection.execute(
            sa.update(table).where(*row_conditions).values(owner=owner, last_verified_at=sa.func.now())
        )


@contextmanager
def _guard_schema_provenance(
    connection: Connection,
    *,
    database_name: str,
    test_only: bool,
    schema_tag: str,
    physical_schema: str | None,
) -> Iterator[None]:
    """Guard against running DDL under a schema that drifted from the
    baseline _register_schema_claim() recorded.

    Read-then-compare: the baseline row must already exist. Checks on
    enter, yields to the caller's DDL block, then refreshes
    last_verified_at (skipped if the block raises). Called only from
    ``guard_schema_provenance_for()``.

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection/transaction the guarded DDL runs on.
    database_name : str
        Identity this provenance record is tracked under. Pass a shared
        identity (e.g. "model_registry") for a schema shared across
        multiple database entries on the same connection.
    test_only : bool
        True skips the check entirely, since a test-only connection's
        schema is expected to change between runs.
    schema_tag : str
        Bookkeeping label this record is tracked under.
    physical_schema : str or None
        The schema to guard, already resolved by the caller.

    Raises
    ------
    SchemaDriftError
        If no baseline row exists for this database_name/schema_tag/
        connection, or the baseline disagrees with *physical_schema*.
    """
    if test_only:
        logger.debug("_guard_schema_provenance(schema_tag=%s): test_only, skipping.", schema_tag)
        yield
        return
    if physical_schema is None:
        # None means this dialect has no schema concept (e.g. SQLite):
        # nothing to protect, nothing to check.
        yield
        return

    connection_safe_url = _connection_safe_url(connection)
    bookkeeping_schema = schema_if_supported(_SCHEMA_PROVENANCE_SCHEMA, connection)
    table = _schema_registry_table(bookkeeping_schema)
    existing_row = None
    if sa.inspect(connection).has_table(SCHEMA_REGISTRY_TABLE_NAME, schema=bookkeeping_schema):
        existing_row = connection.execute(
            sa.select(table.c.physical_schema).where(
                table.c.database_name == database_name,
                table.c.schema_tag == schema_tag,
                table.c.connection_safe_url == connection_safe_url,
            )
        ).first()

    if existing_row is None:
        raise SchemaDriftError(
            f"No schema-registry baseline for database {database_name!r} (schema_tag "
            f"{schema_tag!r}) on this connection. _register_schema_claim()/create_engine() "
            "must run with this claim before it can be guarded."
        )

    stored_schema = existing_row.physical_schema
    if stored_schema != physical_schema:
        raise SchemaDriftError(
            f"Schema drift detected for database {database_name!r} (schema_tag {schema_tag!r}): "
            f"previously resolved to schema {stored_schema!r}, now resolves to "
            f"{physical_schema!r}. Run `acknowledge-schema-migration` once this change is "
            "confirmed deliberate."
        )

    yield

    connection.execute(
        sa.update(table)
        .where(
            table.c.database_name == database_name,
            table.c.schema_tag == schema_tag,
            table.c.connection_safe_url == connection_safe_url,
        )
        .values(last_verified_at=sa.func.now())
    )


def record_schema_provenance(
    connection: Connection,
    *,
    database_name: str,
    schema_tag: str,
    new_physical_schema: str | None,
    reason: str,
) -> None:
    """Overwrite the provenance baseline for database_name/schema_tag.

    Bookkeeping only; no table is moved or dropped here (see
    drop_orphan_schema_tables for that). Overwrites any existing row; its
    prior physical_schema moves into previous_physical_schema.

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection/transaction the update runs on.
    database_name : str
        Identity this provenance record is tracked under.
    schema_tag : str
        Bookkeeping label this record is tracked under.
    new_physical_schema : str or None
        The new baseline schema. None is allowed for a dialect with no
        schema concept.
    reason : str
        Human-readable explanation for the change.

    Raises
    ------
    ValueError
        If reason is blank.
    """
    if not reason.strip():
        raise ValueError("reason must not be blank.")

    connection_safe_url = _connection_safe_url(connection)
    table = _create_schema_registry_table(connection)

    existing_row = connection.execute(
        sa.select(table.c.physical_schema).where(
            table.c.database_name == database_name,
            table.c.schema_tag == schema_tag,
            table.c.connection_safe_url == connection_safe_url,
        )
    ).first()

    if existing_row is not None:
        connection.execute(
            sa.update(table)
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
            sa.insert(table).values(
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


def _find_schema_provenance_claim(
    connection: Connection,
    *,
    physical_schema: str,
    exclude_database_name: str | None = None,
) -> str | None:
    """database_name of the row currently recording physical_schema as its
    baseline on this connection, or None.

    Checks only physical_schema, never previous_physical_schema: a schema
    migrated away from must not be reported as still claimed.

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Only rows for this connection's own URL are considered.
    physical_schema : str
        Physical schema to check for a current claim.
    exclude_database_name : str, optional
        Ignore rows for this database_name. Pass the database being
        acknowledged, since its own primary/vocab/results tags sharing
        one physical schema is not a collision with itself.

    Returns
    -------
    str or None
        The claiming row's database_name, or None.
    """
    if not _has_schema_registry_table(connection):
        return None
    connection_safe_url = _connection_safe_url(connection)
    table = _schema_registry_table(schema_if_supported(_SCHEMA_PROVENANCE_SCHEMA, connection))
    conditions = [
        table.c.connection_safe_url == connection_safe_url,
        table.c.physical_schema == physical_schema,
    ]
    if exclude_database_name is not None:
        conditions.append(table.c.database_name != exclude_database_name)
    row = connection.execute(sa.select(table.c.database_name).where(*conditions)).first()
    return row.database_name if row is not None else None


def _find_reservation_claim(connection: Connection, *, physical_schema: str) -> str | None:
    """Owner reserving physical_schema on this connection, or None.

    Only rows with reserved = True match; a normal schema_tag claim
    sharing this physical schema (e.g. results_schema falling back to
    cdm_schema) is not a reservation.
    """
    if not _has_schema_registry_table(connection):
        return None
    connection_safe_url = _connection_safe_url(connection)
    table = _schema_registry_table(schema_if_supported(_SCHEMA_PROVENANCE_SCHEMA, connection))
    row = connection.execute(
        sa.select(table.c.owner).where(
            table.c.connection_safe_url == connection_safe_url,
            table.c.physical_schema == physical_schema,
            table.c.reserved.is_(True),
        )
    ).first()
    return row.owner if row is not None else None


def _reject_reservation(connection: Connection, *, physical_schema: str | None) -> None:
    """Raise SchemaOwnershipError if physical_schema is reserved by someone on this connection."""
    if physical_schema is None:
        return
    owner = _find_reservation_claim(connection, physical_schema=physical_schema)
    if owner is not None:
        raise SchemaOwnershipError(
            f"Schema {physical_schema!r} is reserved for internal use by {owner!r}."
        )


def physical_schema_of(bindable: Bindable, *, schema_tag: str | None = Role.PRIMARY) -> str | None:
    """Look up schema_tag's physical schema in the bindable's
    schema_translate_map, falling back to the schema_registry table.

    Parameters
    ----------
    bindable : Engine | Connection | Session
    schema_tag : str or None, optional
        The schema_translate_map key to look up. Defaults to Role.PRIMARY.

    Returns
    -------
    str or None
        None if schema_tag is None. The mapped physical schema if
        schema_tag is a schema_translate_map key. schema_tag itself if it
        has no map entry but is registered as a physical schema on this
        connection; None if the dialect has no schema concept.

    Raises
    ------
    UnregisteredSchemaTagError
        If schema_tag has no schema_translate_map entry and is not
        registered as a physical schema either.
    """
    if schema_tag is None:
        return None
    bind = _as_bind(bindable)
    execution_options = bind.get_execution_options()
    stm = execution_options.get(SCHEMA_TRANSLATE_MAP_KEY)
    if stm and schema_tag in stm:
        resolved = stm[schema_tag]
    elif supports_schemas(bind):
        with open_connection(bind) as connection:
            connection_safe_url = _connection_safe_url(connection)
            bookkeeping_schema = schema_if_supported(_SCHEMA_PROVENANCE_SCHEMA, connection)
            table = _schema_registry_table(bookkeeping_schema)
            found = sa.inspect(connection).has_table(
                SCHEMA_REGISTRY_TABLE_NAME, schema=bookkeeping_schema
            ) and connection.execute(
                sa.select(table.c.physical_schema).where(
                    table.c.connection_safe_url == connection_safe_url,
                    table.c.physical_schema == schema_tag,
                )
            ).first() is not None
        if not found:
            raise UnregisteredSchemaTagError(
                f"Schema tag {schema_tag!r} was never registered via create_engine() "
                "on this connection. Check for a typo, or a missing schema_claims entry."
            )
        resolved = schema_tag
    else:
        resolved = schema_tag
    return schema_if_supported(resolved, bind)


def claimed_schema_tags(bindable: Bindable) -> set[str]:
    """Every schema tag bindable's own schema_translate_map execution
    option currently routes.

    Parameters
    ----------
    bindable : Engine | Connection | Session

    Returns
    -------
    set[str]
        Keys of the schema_translate_map execution option, or an empty
        set if none is set.
    """
    bind = _as_bind(bindable)
    stm = bind.get_execution_options().get(SCHEMA_TRANSLATE_MAP_KEY)
    return set(stm) if stm else set()


def _has_schema_registry_table(connection: Connection) -> bool:
    """True if the schema_registry table already exists on this connection."""
    bookkeeping_schema = schema_if_supported(_SCHEMA_PROVENANCE_SCHEMA, connection)
    return sa.inspect(connection).has_table(SCHEMA_REGISTRY_TABLE_NAME, schema=bookkeeping_schema)
