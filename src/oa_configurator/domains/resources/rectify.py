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

from .sql import (
    find_schema_provenance_claim, 
    qualified, 
    reject_reserved_schema, 
    supports_schemas
)

if TYPE_CHECKING:
    from ...stack_config import StackConfig


def _refuse_production_collision(target: sa.URL) -> None:
    """Raise RuntimeError if target's host/database/port matches a non-test_only connection.

    Shared by testing's create-database and drop-database paths, so a
    connection.toml hand-edited to bypass the connections-add-time check
    still gets caught here at provisioning time.
    """
    from ...loader import load_stack_config
    from ...resolver import _find_production_collision

    try:
        config = load_stack_config()
    except (FileNotFoundError, ValueError):
        return
    match = _find_production_collision(target.host, target.database, target.port, config)
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
            count = connection.execute(
                sa.text(f"SELECT COUNT(*) FROM {qualified(connection, name, physical_schema=schema)}")
            ).scalar()
        except Exception:
            count = None
        previews.append(OrphanTablePreview(table_name=name, row_count=count))
    return previews


def schema_is_a_current_target(
    connection: sa.Connection, 
    stack: "StackConfig", 
    schema: str
) -> str | None:
    """Name of a configured database matching schema and connection identity, or None.

    Checks every database in the stack and matches connection by
    host, database and port. Compares the schema against the dialect's
    default schema if the database entry has no explicit schema configured.
    """
    from ...resolver import Resolver

    resolver = Resolver(stack)
    target_url = connection.engine.url
    for name in stack.databases:
        resolved = resolver.resolve_database(name)
        candidate_url = resolved.connection._engine_url
        if (
            candidate_url.host != target_url.host
            or candidate_url.database != target_url.database
            or candidate_url.port != target_url.port
        ):
            continue
        if schema in resolved.occupied_schemas(connection):
            return name
    return None


def drop_orphan_schema_tables(
    connection: sa.Connection,
    *,
    stack: "StackConfig",
    orphan_schema: str,
    confirm: bool,
) -> list[OrphanTablePreview]:
    """Drop every table found in *orphan_schema*, after the cross-entry safety check.

    Preview-only when *confirm* is False: returns what would be dropped
    without touching anything.

    Raises
    ------
    ValueError
        If *connection*'s dialect has no real schema concept at all (e.g.
        SQLite). "Orphan schema" doesn't apply there.
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
    reject_reserved_schema(orphan_schema)
    blocking = schema_is_a_current_target(connection, stack, orphan_schema)
    if blocking is not None:
        raise RuntimeError(
            f"Refusing to drop tables in schema {orphan_schema!r}: it is the current schema "
            f"target of database {blocking!r}. Reconfigure or drop that database entry first "
            "if this schema is genuinely meant to be retired."
        )
    claimant = find_schema_provenance_claim(connection, physical_schema=orphan_schema)
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
        # Accumulate only tables in the orphan schema
        for table in list(metadata.tables.values()):
            if table.schema != orphan_schema:
                metadata.remove(table)
        metadata.drop_all(bind=connection)
    return preview
