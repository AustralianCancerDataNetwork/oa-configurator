"""Schema registry: ownership, reservation, and drift detection.

Tracks which physical schema each schema_translate_map tag resolves to
on each physical database, in a live connection-backed table
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
from collections.abc import Iterable, Iterator
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
    connection_key,
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


class SchemaRegistryOutdatedError(RuntimeError):
    """The schema_registry table on a connection predates its current layout."""


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
    """One schema-registry row: the physical schema schema_tag resolves to
    on one physical database.

    Keyed by (connection_key, schema_tag). One row shape serves three
    checks: drift (compare physical_schema), ownership conflicts (owner),
    and reservation conflicts (rows with reserved set).

    owner is the package claiming the tag and drives the conflict checks.
    database_config_name is the ``[databases.*]`` entry that established the
    mapping and is informational only: it is written on insert and on
    acknowledgment, never on a re-asserted claim, and is shown in drift errors.

    Declared with a fixed schema (``_SCHEMA_PROVENANCE_SCHEMA``) as a
    schema_translate_map token. :func:`_with_provenance_translate_map`
    ensures the connection's own schema_translate_map maps that token
    to the real physical schema.
    """

    __tablename__ = SCHEMA_REGISTRY_TABLE_NAME
    __table_args__ = (
        sa.UniqueConstraint(
            "connection_key",
            "schema_tag",
            name="uq_schema_registry_connection_schema_tag",
        ),
        sa.Index(
            "uq_schema_registry_reserved_connection_schema",
            "connection_key",
            "physical_schema",
            unique=True,
            postgresql_where=sa.text("reserved IS TRUE"),
            sqlite_where=sa.text("reserved IS TRUE"),
        ),
        {"schema": _SCHEMA_PROVENANCE_SCHEMA},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    connection_key: Mapped[str] = mapped_column(sa.String(512))
    schema_tag: Mapped[str] = mapped_column(sa.String(32))
    database_config_name: Mapped[str] = mapped_column(sa.String(128))
    physical_schema: Mapped[str | None] = mapped_column(sa.String(128))
    owner: Mapped[str | None] = mapped_column(sa.String(128))
    reserved: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    first_recorded_at: Mapped[datetime.datetime] = mapped_column(server_default=sa.func.now())
    previous_physical_schema: Mapped[str | None] = mapped_column(sa.String(128))
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column()
    reason: Mapped[str | None] = mapped_column(sa.Text)
    last_verified_at: Mapped[datetime.datetime | None] = mapped_column(server_default=sa.func.now())



def _connection_key(connection: Connection) -> str:
    """Physical identity of *connection*'s database; every row in this table is scoped by it."""
    return connection_key(connection.engine.url)


def _with_provenance_translate_map(connection: Connection, *, physical_schema: str | None) -> Connection:
    """*connection*, guaranteed to map ``_SCHEMA_PROVENANCE_SCHEMA`` to
    *physical_schema* in its own ``schema_translate_map`` execution option,
    merged with whatever entries it already carries.

    Every statement against ``SchemaRegistry`` must run through a 
    connection returned by this function.
    """
    existing = connection.get_execution_options().get(SCHEMA_TRANSLATE_MAP_KEY) or {}
    return connection.execution_options(
        **{SCHEMA_TRANSLATE_MAP_KEY: {**existing, _SCHEMA_PROVENANCE_SCHEMA: physical_schema}}
    )

def _registry_connection(connection: Connection) -> Connection | None:
    """*connection* with the provenance translate map applied, or None if
    the schema_registry table doesn't exist on it yet.
    """
    if not _has_schema_registry_table(connection):
        return None
    return _with_provenance_translate_map(
        connection, physical_schema=schema_if_supported(_SCHEMA_PROVENANCE_SCHEMA, connection)
    )


def _ensure_schema_registry_table(connection: Connection) -> Connection:
    """Create the schema_registry table on this connection if it doesn't
    exist yet. Returns the connection to use for every subsequent
    statement against ``SchemaRegistry.__table__``, carrying the
    schema_translate_map entry that DDL/DML compiled against it needs.
    """
    physical_schema = schema_if_supported(_SCHEMA_PROVENANCE_SCHEMA, connection)
    ensure_schema(connection, physical_schema)
    connection = _with_provenance_translate_map(connection, physical_schema=physical_schema)
    if not _has_schema_registry_table(connection):
        cast(sa.Table, SchemaRegistry.__table__).create(bind=connection)
    return connection

def _reject_ownership_conflict(
    connection: Connection,
    *,
    connection_key: str,
    schema_tag: str,
    physical_schema: str | None,
    owner: str | None,
) -> None:
    """Raise SchemaOwnershipError if a different owner already claims schema_tag
    on this connection with a different physical_schema."""
    conflict = connection.execute(
        sa.select(SchemaRegistry.owner, SchemaRegistry.physical_schema).where(
            SchemaRegistry.connection_key == connection_key,
            SchemaRegistry.schema_tag == schema_tag,
            SchemaRegistry.owner.is_not(None),
            SchemaRegistry.owner != owner,
            SchemaRegistry.physical_schema != physical_schema,
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
    *,
    physical_schema: str | None,
    schema_tag: str | None = None,
    owner: str | None = None,
    reserved: bool = False,
) -> None:
    """Raise SchemaOwnershipError if using physical_schema conflicts with a
    reservation on this connection.

    Any reserved row for physical_schema conflicts; with reserved True, any
    row for it does. The claimant's own rows are excluded: every row of its
    owner, or, without an owner, its schema_tag's ownerless row. No-op when
    physical_schema is None or the schema_registry table doesn't exist yet.

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection the registry is read through.
    physical_schema : str or None
        The physical schema being used.
    schema_tag : str, optional
        The claimant's schema_tag.
    owner : str, optional
        The claimant's owner.
    reserved : bool, optional
        True if the claimant reserves physical_schema itself.

    Raises
    ------
    SchemaOwnershipError
        On a conflicting row.
    """
    registry = _registry_connection(connection)
    if physical_schema is None or registry is None:
        return
    conditions = [
        SchemaRegistry.connection_key == _connection_key(connection),
        SchemaRegistry.physical_schema == physical_schema,
    ]
    if not reserved:
        conditions.append(SchemaRegistry.reserved.is_(True))
    if owner is not None:
        conditions.append(SchemaRegistry.owner.is_distinct_from(owner))
    elif schema_tag is not None:
        conditions.append(sa.or_(SchemaRegistry.schema_tag != schema_tag, SchemaRegistry.owner.is_not(None)))
    conflict = registry.execute(
        sa.select(
            SchemaRegistry.owner,
            SchemaRegistry.schema_tag,
            SchemaRegistry.database_config_name,
            SchemaRegistry.reserved,
        ).where(*conditions)
    ).first()
    if conflict is None:
        return
    holder = (
        f"schema tag {conflict.schema_tag!r} (owner {conflict.owner!r}, established by "
        f"{conflict.database_config_name!r})"
    )
    if conflict.reserved:
        raise SchemaOwnershipError(
            f"Physical schema {physical_schema!r} on this connection is already reserved by {holder}."
        )
    raise SchemaOwnershipError(
        f"Physical schema {physical_schema!r} on this connection is already used by {holder}, "
        "so it cannot be reserved."
    )


def _check_schema_claim(
    connection: Connection,
    *,
    schema_tag: str,
    physical_schema: str | None,
    owner: str | None = None,
    reserved: bool = False,
) -> None:
    """Run the ownership and reservation conflict checks for one claim without writing.

    No-op when physical_schema is None or the schema_registry table doesn't exist yet.

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection the registry is read through.
    schema_tag : str
        The schema_translate_map key being claimed.
    physical_schema : str or None
        The physical schema schema_tag resolves to.
    owner : str, optional
        The claiming package.
    reserved : bool, optional
        True if the claim reserves physical_schema.

    Raises
    ------
    SchemaOwnershipError
        If a different owner already claims schema_tag with a different
        physical_schema, or the claim conflicts with a reservation.
    """
    registry = _registry_connection(connection)
    if physical_schema is None or registry is None:
        return
    _reject_ownership_conflict(
        registry,
        connection_key=_connection_key(connection),
        schema_tag=schema_tag,
        physical_schema=physical_schema,
        owner=owner,
    )
    _reject_reservation_conflict(
        connection,
        physical_schema=physical_schema,
        schema_tag=schema_tag,
        owner=owner,
        reserved=reserved,
    )


def _register_schema_claim(
    connection: Connection,
    *,
    database_config_name: str,
    schema_tag: str,
    physical_schema: str | None,
    owner: str | None = None,
    reserved: bool = False,
) -> None:
    """Claim schema_tag/physical_schema on this physical database for owner,
    checked against every other claim on it.

    Inserts the row on first claim, refreshes owner, reserved and
    last_verified_at when the same physical_schema is re-asserted, and leaves a differing row for
    the guard to report as drift. Called only from create_engine().

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection/transaction the claim is checked and recorded against.
    database_config_name : str
        The ``[databases.*]`` entry making the claim. Recorded only when the
        row is inserted.
    schema_tag : str
        The schema_translate_map key being claimed.
    physical_schema : str or None
        The physical schema schema_tag resolves to. None is a no-op.
    owner : str, optional
        The claiming package. None for the resolver's own Role entries.
    reserved : bool, optional
        True if physical_schema may not be used by any other owner on this
        physical database, regardless of schema_tag.

    Raises
    ------
    SchemaOwnershipError
        If a different owner already claims schema_tag with a different
        physical_schema, or the claim conflicts with a reservation.
    SchemaDriftError
        On first registration, if physical_schema already has tables in
        it. Run `acknowledge-schema-migration` to establish the baseline.
    """
    if physical_schema is None:
        return

    connection_key = _connection_key(connection)
    existing_tables = (
        set(sa.inspect(connection).get_table_names(schema=physical_schema))
        if supports_schemas(connection) else set()
    )
    # The registry's own table is bookkeeping, not pre-existing data.
    if physical_schema == _SCHEMA_PROVENANCE_SCHEMA:
        existing_tables.discard(SCHEMA_REGISTRY_TABLE_NAME)
    already_populated = bool(existing_tables)

    connection = _ensure_schema_registry_table(connection)
    _check_schema_claim(
        connection, schema_tag=schema_tag, physical_schema=physical_schema, owner=owner, reserved=reserved
    )

    row_conditions = (
        SchemaRegistry.connection_key == connection_key,
        SchemaRegistry.schema_tag == schema_tag,
    )
    existing_row = connection.execute(
        sa.select(SchemaRegistry.physical_schema).where(*row_conditions)
    ).first()

    if existing_row is None:
        if already_populated:
            raise SchemaDriftError(
                f"Schema {physical_schema!r} for {database_config_name!r} (schema_tag "
                f"{schema_tag!r}) already has tables, but no schema-registry record exists "
                "for it. Run `acknowledge-schema-migration` to establish a baseline before "
                "proceeding."
            )
        connection.execute(
            sa.insert(SchemaRegistry).values(
                connection_key=connection_key,
                schema_tag=schema_tag,
                database_config_name=database_config_name,
                physical_schema=physical_schema,
                owner=owner,
                reserved=reserved,
            )
        )
    elif existing_row.physical_schema == physical_schema:
        connection.execute(
            sa.update(SchemaRegistry)
            .where(*row_conditions)
            .values(owner=owner, reserved=reserved, last_verified_at=sa.func.now())
        )


@contextmanager
def _guard_schema_provenance(
    connection: Connection,
    *,
    database_config_name: str,
    test_only: bool,
    schema_tag: str,
    physical_schema: str | None,
) -> Iterator[None]:
    """Guard against running DDL under a schema that drifted from the
    baseline _register_schema_claim() recorded for schema_tag on this
    physical database.

    Read-then-compare: the baseline row must already exist. Checks on
    enter, yields to the caller's DDL block, then refreshes
    last_verified_at (skipped if the block raises). Called only from
    ``guard_schema_provenance_for()`` and the ``verify`` CLI.

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection/transaction the guarded DDL runs on.
    database_config_name : str
        The ``[databases.*]`` entry running the DDL. Used in error messages only.
    test_only : bool
        True skips the check entirely, since a test-only connection's
        schema is expected to change between runs.
    schema_tag : str
        The schema_translate_map key being guarded.
    physical_schema : str or None
        The schema to guard, already resolved by the caller.

    Raises
    ------
    SchemaDriftError
        If no baseline row exists for schema_tag on this physical database,
        or the baseline disagrees with *physical_schema*.
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

    connection_key = _connection_key(connection)
    row_conditions = (
        SchemaRegistry.connection_key == connection_key,
        SchemaRegistry.schema_tag == schema_tag,
    )
    no_baseline = SchemaDriftError(
        f"No schema-registry baseline for schema_tag {schema_tag!r} on this database "
        f"({database_config_name!r}). create_engine() must register this claim before "
        "it can be guarded."
    )
    registry = _registry_connection(connection)
    if registry is None:
        raise no_baseline
    existing_row = registry.execute(
        sa.select(SchemaRegistry.physical_schema, SchemaRegistry.database_config_name).where(
            *row_conditions
        )
    ).first()
    if existing_row is None:
        raise no_baseline

    if existing_row.physical_schema != physical_schema:
        raise SchemaDriftError(
            f"Schema drift detected for schema_tag {schema_tag!r}: {database_config_name!r} "
            f"resolves it to {physical_schema!r}, but {existing_row.database_config_name!r} "
            f"established it as {existing_row.physical_schema!r}. Run "
            "`acknowledge-schema-migration` once this change is confirmed deliberate."
        )

    yield

    registry.execute(
        sa.update(SchemaRegistry).where(*row_conditions).values(last_verified_at=sa.func.now())
    )


def _record_schema_provenance(
    connection: Connection,
    *,
    database_config_name: str,
    schema_tag: str,
    new_physical_schema: str | None,
    reason: str,
    exclude_schema_tags: Iterable[str] = (),
) -> None:
    """Overwrite the provenance baseline for schema_tag on this physical database.

    Bookkeeping only; no table is moved or dropped here (see
    drop_orphan_schema_tables for that). The existing row's physical_schema
    moves into previous_physical_schema, database_config_name takes
    ownership of the mapping. reserved is left to create_engine(): an
    existing row keeps it, a new row starts unreserved. Called only from
    the ``acknowledge-schema-migration`` CLI.

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection/transaction the update runs on.
    database_config_name : str
        The ``[databases.*]`` entry taking ownership of the mapping.
    schema_tag : str
        The schema_translate_map key being acknowledged.
    new_physical_schema : str or None
        The new baseline schema. None is allowed for a dialect with no
        schema concept.
    reason : str
        Human-readable explanation for the change.
    exclude_schema_tags : Iterable[str], optional
        Tags other than schema_tag whose rows may already record
        new_physical_schema, e.g. the acknowledged database's own Role tags.

    Raises
    ------
    ValueError
        If reason is blank.
    SchemaOwnershipError
        If new_physical_schema conflicts with a reservation.
    SchemaDriftError
        If another row already records new_physical_schema as its baseline.
    """
    if not reason.strip():
        raise ValueError("reason must not be blank.")

    connection_key = _connection_key(connection)
    connection = _ensure_schema_registry_table(connection)
    row_conditions = (
        SchemaRegistry.connection_key == connection_key,
        SchemaRegistry.schema_tag == schema_tag,
    )

    existing_row = connection.execute(
        sa.select(SchemaRegistry.physical_schema, SchemaRegistry.owner, SchemaRegistry.reserved).where(
            *row_conditions
        )
    ).first()

    if new_physical_schema is not None:
        _reject_reservation_conflict(
            connection,
            physical_schema=new_physical_schema,
            schema_tag=schema_tag,
            owner=existing_row.owner if existing_row is not None else None,
            reserved=existing_row is not None and existing_row.reserved,
        )
        claimant = _find_schema_provenance_claim(
            connection,
            physical_schema=new_physical_schema,
            exclude_schema_tags={*exclude_schema_tags, schema_tag},
        )
        if claimant is not None:
            raise SchemaDriftError(
                f"Refusing to acknowledge {new_physical_schema!r} as the new baseline for "
                f"{database_config_name!r} ({schema_tag!r}): schema-provenance already "
                f"records it as claimed by {claimant!r}."
            )

    if existing_row is not None:
        connection.execute(
            sa.update(SchemaRegistry)
            .where(*row_conditions)
            .values(
                database_config_name=database_config_name,
                previous_physical_schema=existing_row.physical_schema,
                physical_schema=new_physical_schema,
                acknowledged_at=sa.func.now(),
                reason=reason,
                last_verified_at=sa.func.now(),
            )
        )
    else:
        connection.execute(
            sa.insert(SchemaRegistry).values(
                connection_key=connection_key,
                schema_tag=schema_tag,
                database_config_name=database_config_name,
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
    exclude_schema_tags: Iterable[str] = (),
) -> str | None:
    """Describe the row currently recording physical_schema as its baseline
    on this physical database, or return None.

    Checks only physical_schema, never previous_physical_schema: a schema
    migrated away from must not be reported as still claimed.

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Only rows for this connection's physical database are considered.
    physical_schema : str
        Physical schema to check for a current claim.
    exclude_schema_tags : Iterable[str], optional
        Ignore rows for these tags, e.g. the tags of the database being
        acknowledged, whose primary/vocab/results may share one schema.

    Returns
    -------
    str or None
        ``"<database_config_name> (<schema_tag>)"`` of the claiming row, or None.
    """
    registry = _registry_connection(connection)
    if registry is None:
        return None
    row = registry.execute(
        sa.select(SchemaRegistry.database_config_name, SchemaRegistry.schema_tag).where(
            SchemaRegistry.connection_key == _connection_key(connection),
            SchemaRegistry.physical_schema == physical_schema,
            SchemaRegistry.schema_tag.not_in(list(exclude_schema_tags)),
        )
    ).first()
    return f"{row.database_config_name} ({row.schema_tag})" if row is not None else None


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
            connection_key = _connection_key(connection)
            registry = _registry_connection(connection)
            found = registry is not None and registry.execute(
                sa.select(SchemaRegistry.physical_schema).where(
                    SchemaRegistry.connection_key == connection_key,
                    SchemaRegistry.physical_schema == schema_tag,
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
    """True if the schema_registry table already exists on this connection.

    Raises
    ------
    SchemaRegistryOutdatedError
        If the existing table is not keyed by ``connection_key``.
    """
    bookkeeping_schema = schema_if_supported(_SCHEMA_PROVENANCE_SCHEMA, connection)
    inspector = sa.inspect(connection)
    if not inspector.has_table(SCHEMA_REGISTRY_TABLE_NAME, schema=bookkeeping_schema):
        return False
    columns = {column["name"] for column in inspector.get_columns(SCHEMA_REGISTRY_TABLE_NAME, schema=bookkeeping_schema)}
    if "connection_key" not in columns:
        table = f"{bookkeeping_schema}.{SCHEMA_REGISTRY_TABLE_NAME}" if bookkeeping_schema else SCHEMA_REGISTRY_TABLE_NAME
        raise SchemaRegistryOutdatedError(
            f"{table} has an outdated layout. Drop it; create_engine() registers every claim again."
        )
    return True
