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

from .sql import qualified, schema_inspect, supports_schemas

if TYPE_CHECKING:
    from ...stack_config import StackConfig


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
    table_names = schema_inspect(connection, schema=schema).get_table_names()
    previews = []
    for name in table_names:
        try:
            count = connection.execute(
                sa.text(f"SELECT COUNT(*) FROM {qualified(connection, name, schema=schema)}")
            ).scalar()
        except Exception:
            count = None
        previews.append(OrphanTablePreview(table_name=name, row_count=count))
    return previews


def schema_is_a_current_target(stack: "StackConfig", schema: str) -> str | None:
    """Return the name of a configured database whose current schema (or
    vocab/results schema) equals schema, or None.

    Checks every database in the stack, not just the one named on the
    command line, since two CDM databases can legitimately share one
    vocabulary schema; dropping tables there would destroy a database a
    different entry is actively using.
    """
    from ...resolver import Resolver
    from .schema import ResolvedCDMDatabase

    resolver = Resolver(stack)
    for name in stack.databases:
        try:
            resolved = resolver.resolve_database(name)
        except Exception:
            continue
        candidates: set[str | None] = {resolved.schema_name}
        if isinstance(resolved, ResolvedCDMDatabase):
            candidates.add(resolved.vocab_schema)
            candidates.add(resolved.results_schema)
        if schema in candidates:
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
        If *orphan_schema* is the current schema target of any configured
        database/role in *stack*.
    """
    if not supports_schemas(connection):
        raise ValueError(
            f"Cannot drop orphan schema tables: {connection.dialect.name!r} has no real schema "
            "concept, so 'orphan schema' doesn't apply and this operation isn't meaningful here."
        )
    blocking = schema_is_a_current_target(stack, orphan_schema)
    if blocking is not None:
        raise RuntimeError(
            f"Refusing to drop tables in schema {orphan_schema!r}: it is the current schema "
            f"target of database {blocking!r}. Reconfigure or drop that database entry first "
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
