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
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NamedTuple, cast

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .sql import (
    SCHEMA_TRANSLATE_MAP_KEY,
    Bindable,
    Dialect,
    Role,
    _as_bind,
    _profile_for,
    ensure_schema,
    connection_key,
    schema_if_supported,
    supports_schemas,
)

logger = logging.getLogger(__name__)

# Registered as reserved in create_engine()
_SCHEMA_PROVENANCE_SCHEMA = "oa_configurator_provenance"

_ROLE_TAG_VALUES = frozenset(member.value for member in Role)

# Advisory-lock key for the registry's bootstrap/registration critical
# section. Scoped per physical database already, so one constant suffices.
_REGISTRY_LOCK_KEY = 0x6f61636667  # oacf -> oa_configurator


def _lock_schema_registry(connection: Connection) -> None:
    """Serialize concurrent first-time create_engine() calls against this
    physical database.

    Postgres's CREATE SCHEMA IF NOT EXISTS and the first insert into a
    fresh table both race under concurrent transactions. No-op on a
    dialect with no advisory locks.

    connection must not be AUTOCOMMIT (the lock releases after one
    statement) and should use at most READ COMMITTED isolation.
    """
    dialect = Dialect(connection.dialect.name)  # raises ValueError on an unmodeled dialect
    if dialect == Dialect.POSTGRESQL:
        connection.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _REGISTRY_LOCK_KEY})
    elif dialect != Dialect.SQLITE:
        raise ValueError(f"No registry-lock strategy defined for dialect {dialect!r}.")


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
        # Role-tag rows ("has *this entry's* mapping changed") are scoped
        # per entry: two different entries (e.g. a CDM database and its own
        # colocated vector store) may each claim Role.PRIMARY on one
        # connection without colliding. Custom tags keep the original,
        # connection-wide scoping: one claim per tag per connection.
        #
        # This scopes the shared bookkeeping row only: each entry's own
        # schema_translate_map lives on its own Engine and never collides.
        sa.Index(
            "uq_schema_registry_role_tag",
            "connection_key",
            "database_config_name",
            "schema_tag",
            unique=True,
            postgresql_where=sa.text(
                "schema_tag IN (" + ", ".join(f"'{v}'" for v in sorted(_ROLE_TAG_VALUES)) + ")"
            ),
            sqlite_where=sa.text(
                "schema_tag IN (" + ", ".join(f"'{v}'" for v in sorted(_ROLE_TAG_VALUES)) + ")"
            ),
        ),
        sa.Index(
            "uq_schema_registry_custom_tag",
            "connection_key",
            "schema_tag",
            unique=True,
            postgresql_where=sa.text(
                "schema_tag NOT IN (" + ", ".join(f"'{v}'" for v in sorted(_ROLE_TAG_VALUES)) + ")"
            ),
            sqlite_where=sa.text(
                "schema_tag NOT IN (" + ", ".join(f"'{v}'" for v in sorted(_ROLE_TAG_VALUES)) + ")"
            ),
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

class RegistryRow(NamedTuple):
    """One schema_registry row, as surfaced by :func:`_list_registry_rows`."""

    schema_tag: str
    physical_schema: str | None
    database_config_name: str
    owner: str | None
    reserved: bool

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
    exist yet. Returns the connection to use for every subsequent statement
    against ``SchemaRegistry.__table__``, carrying the schema_translate_map
    entry that DDL/DML compiled against it needs.
    """
    physical_schema = schema_if_supported(_SCHEMA_PROVENANCE_SCHEMA, connection)
    ensure_schema(connection, physical_schema)
    connection = _with_provenance_translate_map(connection, physical_schema=physical_schema)
    if not _has_schema_registry_table(connection):
        cast(sa.Table, SchemaRegistry.__table__).create(bind=connection)
    return connection


def _row_conditions(
    connection_key: str, schema_tag: str, database_config_name: str
) -> tuple[Any, ...]:
    """The predicate identifying *schema_tag*'s own registry row on this
    physical database.

    A Role tag's row is scoped per entry (``database_config_name`` is part
    of the predicate): two different entries may each claim the same Role
    tag on one connection without colliding. A custom tag's row stays
    scoped per connection only, matching :class:`SchemaRegistry`'s indexes.
    """
    conditions: tuple[Any, ...] = (
        SchemaRegistry.connection_key == connection_key,
        SchemaRegistry.schema_tag == schema_tag,
    )
    if schema_tag in _ROLE_TAG_VALUES:
        conditions = (*conditions, SchemaRegistry.database_config_name == database_config_name)
    return conditions


def _reject_ownership_conflict(
    connection: Connection,
    *,
    connection_key: str,
    schema_tag: str,
    physical_schema: str | None,
    owner: str | None,
) -> None:
    """Raise SchemaOwnershipError if a different owner already claims schema_tag
    on this connection."""
    conflict = connection.execute(
        sa.select(SchemaRegistry.owner, SchemaRegistry.physical_schema).where(
            SchemaRegistry.connection_key == connection_key,
            SchemaRegistry.schema_tag == schema_tag,
            SchemaRegistry.owner.is_not(None),
            SchemaRegistry.owner != owner,
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
    database_config_name: str,
    schema_tag: str,
    physical_schema: str | None,
    owner: str | None = None,
    reserved: bool = False,
    test_only: bool = False,
) -> None:
    """Run the ownership, reservation, and drift checks for one claim
    without writing.

    No-op when physical_schema is None or the schema_registry table doesn't
    exist yet (nothing has ever registered this claim, so there's nothing
    to drift from).

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection the registry is read through.
    database_config_name : str
        The ``[databases.*]`` entry making the claim.
    schema_tag : str
        The schema_translate_map key being claimed.
    physical_schema : str or None
        The physical schema schema_tag resolves to.
    owner : str, optional
        The claiming package.
    reserved : bool, optional
        True if the claim reserves physical_schema.
    test_only : bool, optional
        True skips the drift check: a test-only connection's schema is
        expected to change between runs.

    Raises
    ------
    SchemaOwnershipError
        If a different owner already claims schema_tag with a different
        physical_schema, or the claim conflicts with a reservation.
    SchemaDriftError
        If an existing baseline row for schema_tag disagrees with
        physical_schema and test_only is False.
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
    if test_only:
        return
    row_conditions = _row_conditions(_connection_key(connection), schema_tag, database_config_name)
    existing_row = registry.execute(
        sa.select(SchemaRegistry.physical_schema, SchemaRegistry.database_config_name).where(
            *row_conditions
        )
    ).first()
    if existing_row is not None and existing_row.physical_schema != physical_schema:
        raise SchemaDriftError(
            f"Schema drift detected for schema_tag {schema_tag!r}: {database_config_name!r} "
            f"resolves it to {physical_schema!r}, but {existing_row.database_config_name!r} "
            f"established it as {existing_row.physical_schema!r}. Run "
            "`acknowledge-schema-migration` once this change is confirmed deliberate."
        )


def _register_schema_claim(
    connection: Connection,
    *,
    database_config_name: str,
    schema_tag: str,
    physical_schema: str | None,
    owner: str | None = None,
    reserved: bool = False,
    test_only: bool = False,
) -> None:
    """Claim schema_tag/physical_schema on this physical database for owner,
    checked against every other claim on it.

    - Inserts the row on first claim, 
    - refreshes owner and reserved when the same physical_schema is re-asserted, 
    - re-baselines a differing row when test_only is True, 
    - raises on a differing row when test_only is False. 

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Open connection/transaction the claim is checked and recorded
        against. Must already carry the registry's own
        schema_translate_map entry (i.e. have been returned by
        ``_ensure_schema_registry_table``).
    database_config_name : str
        The ``[databases.*]`` entry making the claim. Recorded only when the
        row is inserted; part of the row's own identity for a Role tag.
    schema_tag : str
        The schema_translate_map key being claimed.
    physical_schema : str or None
        The physical schema schema_tag resolves to. None is a no-op.
    owner : str, optional
        The claiming package. None for the resolver's own Role entries.
    reserved : bool, optional
        True if physical_schema may not be used by any other owner on this
        physical database, regardless of schema_tag.
    test_only : bool, optional
        True skips every raise below in favour of registering/re-baselining
        directly: a test-only connection's schema is expected to change
        between runs, and has no production data to protect.

    Raises
    ------
    SchemaOwnershipError
        If a different owner already claims schema_tag with a different
        physical_schema, or the claim conflicts with a reservation.
    SchemaDriftError
        If this is a Role tag's first registration and physical_schema
        already has tables in it with no baseline row (run
        `acknowledge-schema-migration` to establish one), or if an existing
        row disagrees with physical_schema and test_only is False.
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
    
    # Idempotent and cheap if the table already exists
    # Allows standalone usage of this function 
    connection = _ensure_schema_registry_table(connection)

    _check_schema_claim(
        connection,
        database_config_name=database_config_name,
        schema_tag=schema_tag,
        physical_schema=physical_schema,
        owner=owner,
        reserved=reserved,
        test_only=test_only,
    )

    row_conditions = _row_conditions(connection_key, schema_tag, database_config_name)
    existing_row = connection.execute(
        sa.select(SchemaRegistry.physical_schema).where(*row_conditions)
    ).first()

    if existing_row is None:
        if already_populated and not test_only and schema_tag in _ROLE_TAG_VALUES:
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
            .values(owner=owner, reserved=reserved)
        )
    elif test_only:
        connection.execute(
            sa.update(SchemaRegistry)
            .where(*row_conditions)
            .values(physical_schema=physical_schema, owner=owner, reserved=reserved)
        )
    else:
        raise SchemaDriftError(
            f"Schema drift detected for schema_tag {schema_tag!r}: {database_config_name!r} "
            f"now resolves it to {physical_schema!r}, but the registry recorded "
            f"{existing_row.physical_schema!r}. Run `acknowledge-schema-migration` once this "
            "change is confirmed deliberate."
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

    Read-then-compare: the baseline row must already exist. Called only
    from ``guard_schema_provenance_for()`` and the ``verify`` CLI.

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
    row_conditions = _row_conditions(connection_key, schema_tag, database_config_name)
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


def _record_schema_provenance(
    connection: Connection,
    *,
    database_config_name: str,
    schema_tag: str,
    new_physical_schema: str | None,
    reason: str,
) -> None:
    """Overwrite the provenance baseline for schema_tag on this physical database.

    Bookkeeping only; no table is moved or dropped here (see
    drop_orphan_schema_tables for that). The existing row's physical_schema
    moves into previous_physical_schema, database_config_name takes
    ownership of the mapping. reserved is left to create_engine(): an
    existing row keeps it, a new row starts unreserved. Called only from
    the ``acknowledge-schema-migration`` CLI; a caller acknowledging several
    tags of one entry may call this repeatedly on one already-open
    connection/transaction.

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
    _lock_schema_registry(connection)
    connection = _ensure_schema_registry_table(connection)
    row_conditions = _row_conditions(connection_key, schema_tag, database_config_name)

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
            schema_tag=schema_tag,
            database_config_name=database_config_name,
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
    schema_tag: str | None = None,
    database_config_name: str | None = None,
) -> str | None:
    """Describe the row currently recording physical_schema as its baseline
    on this physical database, or return None.

    Checks only physical_schema, never previous_physical_schema: a schema
    migrated away from must not be reported as still claimed.

    Notes
    -----
    - acknowledge-schema-migration: With schema_tag and database_config_name both given, 
    excludes the row being acknowledged itself. If schema_tag is a Role tag, every other
    Role-tag row of the same database_config_name is also excluded: an entry's own
    primary/vocab/results may legitimately share one physical schema.
    - drop-orphan-schema-tables: With either schema_tag or database_config_name omitted,
    no row is excluded: any claimant at all is reported.

    Parameters
    ----------
    connection : sqlalchemy.engine.Connection
        Only rows for this connection's physical database are considered.
    physical_schema : str
        Physical schema to check for a current claim.
    schema_tag : str, optional
        The tag being acknowledged; excluded from the search.
    database_config_name : str, optional
        The entry being acknowledged; its own Role-tag rows are excluded
        from the search when schema_tag is a Role tag.

    Returns
    -------
    str or None
        ``"<database_config_name> (<schema_tag>)"`` of the claiming row, or None.
    """
    registry = _registry_connection(connection)
    if registry is None:
        return None
    connection_key = _connection_key(connection)
    exclusions: list[Any] = []
    if schema_tag is not None and database_config_name is not None:
        exclusions.append(sa.and_(*_row_conditions(connection_key, schema_tag, database_config_name)))
    if schema_tag in _ROLE_TAG_VALUES and database_config_name is not None:
        exclusions.append(sa.and_(
            SchemaRegistry.connection_key == connection_key,
            SchemaRegistry.database_config_name == database_config_name,
            SchemaRegistry.schema_tag.in_(_ROLE_TAG_VALUES),
        ))
    row = registry.execute(
        sa.select(SchemaRegistry.database_config_name, SchemaRegistry.schema_tag).where(
            SchemaRegistry.connection_key == connection_key,
            SchemaRegistry.physical_schema == physical_schema,
            sa.not_(sa.or_(*exclusions)) if exclusions else sa.true(),
        )
    ).first()
    return f"{row.database_config_name} ({row.schema_tag})" if row is not None else None



def _list_registry_rows(connection: Connection) -> tuple[RegistryRow, ...]:
    """Every registry row for this connection's physical database.
    """
    registry = _registry_connection(connection)
    if registry is None:
        return ()
    rows = registry.execute(
        sa.select(
            SchemaRegistry.schema_tag,
            SchemaRegistry.physical_schema,
            SchemaRegistry.database_config_name,
            SchemaRegistry.owner,
            SchemaRegistry.reserved,
        ).where(SchemaRegistry.connection_key == _connection_key(connection))
    ).all()
    return tuple(RegistryRow(*row) for row in rows)


def _release_schema_claim(connection: Connection, *, schema_tag: str, database_config_name: str) -> bool:
    """Delete schema_tag's registry row for database_config_name on this
    physical database. Returns True if a row was deleted, False if none
    existed."""
    _lock_schema_registry(connection)
    connection = _ensure_schema_registry_table(connection)
    row_conditions = _row_conditions(_connection_key(connection), schema_tag, database_config_name)
    result = connection.execute(sa.delete(SchemaRegistry).where(*row_conditions))
    return result.rowcount > 0


def physical_schema_of(bindable: Bindable, *, schema_tag: str | None = Role.PRIMARY) -> str | None:
    """Look up schema_tag's physical schema in the bindable's
    schema_translate_map.

    Parameters
    ----------
    bindable : Engine | Connection | Session
    schema_tag : str or None, optional
        The schema_translate_map key to look up. Defaults to Role.PRIMARY.

    Returns
    -------
    str or None
        None if schema_tag is None, or if the dialect has no schema
        concept. Otherwise the mapped physical schema.

    Raises
    ------
    UnregisteredSchemaTagError
        If schema_tag has no schema_translate_map entry on a
        schema-capable dialect. There used to be a fallback here treating
        schema_tag as a literal, already-registered physical schema name;
        it was removed because it silently accepted a typo'd tag that
        happened to collide with some other tag's physical name.
    """
    if schema_tag is None:
        return None
    bind = _as_bind(bindable)
    execution_options = bind.get_execution_options()
    stm = execution_options.get(SCHEMA_TRANSLATE_MAP_KEY)
    if stm and schema_tag in stm:
        resolved = stm[schema_tag]
    elif supports_schemas(bind):
        raise UnregisteredSchemaTagError(
            f"Schema tag {schema_tag!r} was never registered via create_engine() "
            "on this connection. Check for a typo, or a missing schema_claims entry."
        )
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
