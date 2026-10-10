"""Acknowledge a deliberate schema change, or clean up orphaned tables left
behind by one.

Generic over any resolvable database, not tied to any particular domain
package's own table metadata. Every action requires an explicit target and
confirmation; nothing here auto-detects or auto-deletes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from .schema_registry import (
    _find_schema_provenance_claim,
    _reject_reservation_conflict,
)
from .sql import _profile_for, qualified, supports_schemas

if TYPE_CHECKING:
    from ...stack_config import StackConfig


def _refuse_production_collision(target: sa.URL) -> None:
    """Raise RuntimeError if target addresses the same physical database as a
    non-test_only connection.

    Shared by testing's create-database and drop-database paths, so a
    connection.toml hand-edited to bypass the connections-add-time check
    still gets caught here at provisioning time.

    Notes
    -----
    An unreadable or invalid config raises, since this guard runs
    immediately before CREATE DATABASE or DROP DATABASE.
    """
    from ...loader import load_stack_config
    from ...resolver import _find_production_collision

    try:
        config = load_stack_config()
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeError(
            f"Refusing to target {target.database!r}: the config could not be read, so "
            "this cannot be checked against production connections. Fix the config, or "
            "point OA_CONFIG_PATH at the intended one, and retry."
        ) from exc
    try:
        match = _find_production_collision(target, config)
    except ValueError as exc:
        raise RuntimeError(
            f"Refusing to target {target.database!r}: production safety could not be verified."
        ) from exc
    if match is not None:
        raise RuntimeError(
            f"Refusing to target {target.database!r}: matches non-test connection {match!r} "
            "(same host, database, and port)."
        )


@dataclass(frozen=True)
class OrphanTablePreview:
    """One table physically present in a candidate orphan schema, with its row count."""

    table_name: str
    row_count: int | None


def preview_orphan_schema_tables(connection: sa.Connection, schema: str) -> list[OrphanTablePreview]:
    """List tables physically present in *schema*, with an exact row count.

    A real ``COUNT(*)`` rather than an approximation: this preview exists to
    inform a destructive drop, and a stale catalog estimate is worse than a
    table scan here. ``row_count`` is ``None`` only if counting itself fails
    (e.g. a view rather than a real table).
    """
    table_names = sa.inspect(connection).get_table_names(schema=schema)
    previews = []
    for name in table_names:
        try:
            with connection.begin_nested():
                count = connection.execute(
                    sa.text(f"SELECT COUNT(*) FROM {qualified(connection, name, physical_schema=schema)}")
                ).scalar()
        except SQLAlchemyError:
            count = None
        previews.append(OrphanTablePreview(table_name=name, row_count=count))
    return previews


def schema_is_a_current_target(
    connection: sa.Connection,
    stack: StackConfig,
    schema: str
) -> str | None:
    """Is this schema still a live target of some configured database's
    Role-tagged schema?

    occupied_schemas() itself determines whether connection corresponds to
    any of a database's own roles. A database with no role on this
    physical server never matches, regardless of whether its own schema
    names happen to coincide.

    Notes
    -----
    A custom, non-Role schema_claims entry (e.g. a package reserving its own
    schema at create_engine() time) is invisible here, since it has no
    StackConfig field to read. drop_orphan_schema_tables catches those
    separately, via the schema_registry table.
    """
    from ...resolver import Resolver

    resolver = Resolver(stack)
    for name in stack.databases:
        resolved = resolver.resolve_database(name)
        if schema in resolved.occupied_schemas(connection):
            return name
    return None


def drop_orphan_schema_tables(
    connection: sa.Connection,
    *,
    stack: StackConfig,
    orphan_schema: str,
    confirm: bool,
    allow_default_schema: bool = False,
) -> list[OrphanTablePreview]:
    """Drop every table found in *orphan_schema*, after the cross-entry safety check.

    Preview-only when *confirm* is False: returns what would be dropped
    without touching anything. Performed using literal, qualified SQL 
    instead of ``metadata.drop_all``: an orphan_schema that happens
    to equal a schema_translate_map key (e.g. a legacy physical schema
    literally named ``"vocab"``) would silently drop the *configured* schema.

    Notes
    -----
    Cover the entire verification before dropping any tables:
    - schema_is_a_current_target() reads *stack*'s static config (Role-tagged schemas
    only, whether or not create_engine() has ever run for them)
    - _reject_reservation_conflict()/_find_schema_provenance_claim() read the live schema_registry
     table (any tag, reserved or not, but only once actually registered by create_engine())

    Raises
    ------
    ValueError
        If *connection*'s dialect has no real schema concept at all (e.g.
        SQLite). "Orphan schema" doesn't apply there. Also raised if
        *orphan_schema* is a dialect system schema, or the dialect's own
        default schema with *allow_default_schema* not set.
    RuntimeError
        If *orphan_schema* is reserved by a registered package, is the
        current schema target of any configured database/role in *stack*,
        or is currently claimed by a schema-provenance record (e.g. a
        shared resource such as a model registry).
    """
    if not supports_schemas(connection):
        raise ValueError(
            f"Cannot drop orphan schema tables: {connection.dialect.name!r} has no real schema "
            "concept, so 'orphan schema' doesn't apply and this operation isn't meaningful here."
        )
    profile = _profile_for(connection.dialect.name)
    if orphan_schema in profile.system_schemas:
        raise ValueError(f"Refusing to treat system schema {orphan_schema!r} as an orphan.")
    if (
        orphan_schema in {sa.inspect(connection).default_schema_name, profile.default_schema}
        and not allow_default_schema
    ):
        raise ValueError(
            f"Refusing to treat the dialect's default schema {orphan_schema!r} as an orphan. "
            "Pass allow_default_schema=True if this is genuinely intended."
        )
    _reject_reservation_conflict(connection, physical_schema=orphan_schema)
    blocking = schema_is_a_current_target(connection, stack, orphan_schema)
    if blocking is not None:
        raise RuntimeError(
            f"Refusing to drop tables in schema {orphan_schema!r}: it is the current schema "
            f"target of database {blocking!r}. Reconfigure or drop that database entry first "
            "if this schema is genuinely meant to be retired."
        )
    claimant = _find_schema_provenance_claim(connection, physical_schema=orphan_schema)
    if claimant is not None:
        raise RuntimeError(
            f"Refusing to drop tables in schema {orphan_schema!r}: schema-provenance records "
            f"it as currently claimed by {claimant!r}. Reconfigure or retire that entry first "
            "if this schema is genuinely meant to be retired."
        )
    preview = preview_orphan_schema_tables(connection, orphan_schema)
    if confirm:
        metadata = sa.MetaData()
        metadata.reflect(bind=connection, schema=orphan_schema)
        for table in reversed(metadata.sorted_tables):
            if table.schema != orphan_schema:
                continue
            connection.execute(sa.text(
                f"DROP TABLE IF EXISTS {qualified(connection, table.name, physical_schema=orphan_schema)}"
            ))
    return preview
