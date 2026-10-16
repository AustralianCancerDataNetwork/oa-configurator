# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "oa-configurator[postgres]>=2.0.0,<3",
#     "omop-emb>=3.0.0,<4",
#     "tomlkit>=0.13",
# ]
# ///
"""Upgrade a 1.x stack (config, schema registry, omop-emb registries) to 2.0.

Dry run by default: prints every change and touches nothing. ``--apply``
backs up first, then makes the changes. Re-running is a no-op.

Old layouts are read with plain SQL and TOML. Only 2.x libraries are imported.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import tomlkit
from omop_emb.config import MODEL_REGISTRY_SCHEMA
from omop_emb.model_registry.model_registry_orm import (
    ModelRegistry,
    _is_legacy_layout,
    registry_reader_engine,
    registry_writer_engine,
    resolve_registry_physical_schema,
)
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy.exc import SQLAlchemyError

from oa_configurator import (
    SCHEMA_REGISTRY_SCHEMA,
    ConnectionConfig,
    Dialect,
    Resolver,
    StackConfig,
    active_config_path,
    find_table_in_other_schemas,
    is_ephemeral_url,
    qualified,
    schema_if_supported,
    supports_schemas,
)
from oa_configurator.domains.resources.schema import ResolvedConnection
from oa_configurator.domains.resources.schema_registry import (
    SCHEMA_REGISTRY_TABLE_NAME,
    SchemaRegistry,
    _ensure_schema_registry_table,
    _list_registry_rows,
    _record_schema_provenance,
    _registry_connection,
    _with_provenance_translate_map,
)
from oa_configurator.domains.resources.sql import Role
from oa_configurator.io import _copy_secure_temp, _fsync_directory, _write_secure_temp
from oa_configurator.package_base import StackConfigInvalidError


class Names:
    """Fixed identifiers and messages used across the script."""

    REASON = "upgrade to 2.0 (to_v2.py)"
    REGISTRY_TABLE = "model_registry"
    LEGACY_TABLE = "model_registry_legacy_v1"
    SUPPORTED_DIALECTS = (Dialect.POSTGRESQL, Dialect.SQLITE)


@dataclass
class Action:
    """One planned change.

    Parameters
    ----------
    label : str
        Human-readable description printed in the diff.
    run : Callable[[], None]
        Applies the change.
    """

    label: str
    run: Callable[[], None]


@dataclass
class Plan:
    """Planned changes and the reasons the run cannot proceed.

    Parameters
    ----------
    actions : list[Action]
        Changes in execution order.
    notes : list[str]
        Informational lines (skips, warnings).
    problems : list[str]
        Conditions that stop the run before anything is changed.
    """

    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def extend(self, other: Plan) -> None:
        self.actions += other.actions
        self.notes += other.notes
        self.problems += other.problems


console = Console(highlight=False)


# --------------------------------------------------------------------------
# Step 1: config file
# --------------------------------------------------------------------------


def find_values(node: Any, value: str, path: str = "") -> list[str]:
    """Dotted paths under *node* whose scalar value equals *value*."""
    if isinstance(node, dict):
        return [hit for key, child in node.items() for hit in find_values(child, value, f"{path}.{key}" if path else str(key))]
    if isinstance(node, list):
        return [hit for i, child in enumerate(node) for hit in find_values(child, value, f"{path}[{i}]")]
    return [path] if node == value else []


def plan_config(text: str, renames: dict[str, str], path: Path) -> tuple[str, StackConfig | None, Plan]:
    """Rewrite *text* to the 2.0 config shape and validate it.

    Parameters
    ----------
    text : str
        Current ``config.toml`` content.
    renames : dict[str, str]
        ``[databases.*]`` entry renames, old name to new name.
    path : pathlib.Path
        Config file, named in validation errors.

    Returns
    -------
    tuple[str, StackConfig or None, Plan]
        The new TOML text, the validated config (None when invalid), and the
        change list. Each change line is a note; the write is added by the caller.
    """
    plan = Plan()
    doc = tomlkit.parse(text)
    connections = doc.get("connections", {})
    databases = doc.get("databases", {})

    for old, new in renames.items():
        if old in databases and new in databases:
            plan.problems.append(f"config: cannot rename {old!r} to {new!r}, both exist.")
        elif old in databases:
            databases[new] = databases.pop(old)
            plan.notes.append(f"config: rename [databases.{old}] -> [databases.{new}]")
            for store_name, store in doc.get("vector_stores", {}).items():
                if store.get("database") == old:
                    store["database"] = new
                    plan.notes.append(f"config: vector_stores.{store_name}.database {old!r} -> {new!r}")
            for hit in find_values(doc.unwrap().get("tools", {}), old):
                plan.notes.append(f"config: WARNING tools.{hit} still names {old!r}; update it by hand.")
        elif new not in databases:
            plan.problems.append(f"config: --rename {old}={new}: neither entry exists.")

    for name, entry in databases.items():
        if entry.get("kind") != "cdm":
            continue
        connection = connections.get(entry.get("connection"))
        if connection is None:
            continue
        try:
            dialect = ConnectionConfig.model_validate(connection.unwrap()).dialect_name
        except ValidationError as exc:
            plan.problems.append(f"config: connection of databases.{name}: {StackConfigInvalidError(path, exc)}")
            continue
        has_old, has_new = "schema_name" in entry, "cdm_schema" in entry
        if has_old and has_new:
            plan.problems.append(f"config: databases.{name} has both schema_name and cdm_schema.")
        elif dialect == Dialect.POSTGRESQL:
            if has_old:
                entry["cdm_schema"] = entry.pop("schema_name")
                plan.notes.append(f"config: databases.{name}: schema_name -> cdm_schema = {entry['cdm_schema']!r}")
            elif not has_new:
                entry["cdm_schema"] = "omop"
                plan.notes.append(f'config: databases.{name}: no schema field, write cdm_schema = "omop"')
        elif dialect == Dialect.SQLITE:
            if has_old:
                del entry["schema_name"]
                plan.notes.append(f"config: databases.{name}: delete schema_name (SQLite takes no schema)")
        elif has_old:
            plan.problems.append(
                f"config: databases.{name} uses dialect {dialect!r}; 1.x only supported "
                f"{', '.join(Names.SUPPORTED_DIALECTS)}. Rename schema_name to cdm_schema by hand."
            )

    new_text = tomlkit.dumps(doc)
    try:
        stack = StackConfig.model_validate(doc.unwrap())
    except ValidationError as exc:
        plan.problems.append(f"config: does not validate against 2.0 StackConfig. {StackConfigInvalidError(path, exc)}")
        stack = None
    return new_text, stack, plan


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------


def sqlite_file(connection: ResolvedConnection) -> Path | None:
    """The SQLite file behind *connection*, or None for other or in-memory databases."""
    url = sa.make_url(connection.url)
    if url.get_backend_name() != Dialect.SQLITE or is_ephemeral_url(url):
        return None
    return Path(url.database)


def connections_of(resolved: Any) -> list[ResolvedConnection]:
    """Distinct physical connections a database entry uses: primary, plus vocab on a CDM entry."""
    roles = {
        resolved.route_for_schema_tag(role.value, vocab=Role.VOCAB, primary=Role.PRIMARY)
        for role in resolved.schema_tags()
    }
    return [resolved.connection_for_role(role) for role in sorted(roles)]


def is_openable(connection: ResolvedConnection) -> bool:
    """False for a SQLite file that does not exist yet; opening it would create it."""
    path = sqlite_file(connection)
    return path is None or path.exists()


def columns_of(inspector: sa.Inspector, table: str, schema: str | None) -> set[str] | None:
    """Column names of *table* in *schema*, or None when it does not exist."""
    if not inspector.has_table(table, schema=schema):
        return None
    return {column["name"] for column in inspector.get_columns(table, schema=schema)}


def registry_layout(connection: sa.Connection) -> str:
    """Layout of the schema registry on *connection*.

    Returns
    -------
    str
        ``absent``, ``current``, ``connection_key`` (a pre-release layout
        with an extra column), or ``unknown``.
    """
    inspector = sa.inspect(connection)
    schema = schema_if_supported(SCHEMA_REGISTRY_SCHEMA, connection)
    columns = columns_of(inspector, SCHEMA_REGISTRY_TABLE_NAME, schema)
    expected = {column.name for column in SchemaRegistry.__table__.columns}
    if columns is None:
        return "absent"
    if columns == expected:
        return "current"
    if columns == expected | {"connection_key"}:
        return "connection_key"
    return "unknown"


# --------------------------------------------------------------------------
# Step 2: schema-registry baselines
# --------------------------------------------------------------------------


def rebuild_registry(
    connection: sa.Connection, current_by_key: dict[tuple[str, str], str | None]
) -> list[str]:
    """Replace a ``connection_key``-layout registry with the 2.0 layout.

    Per ``(database_config_name, schema_tag)`` keeps the row whose
    ``physical_schema`` matches the current resolution, else the first.

    Parameters
    ----------
    connection : sqlalchemy.Connection
        Open connection, inside a transaction.
    current_by_key : dict
        ``(database_config_name, schema_tag)`` to the currently configured schema.

    Returns
    -------
    list[str]
        Warnings for keys whose rows disagreed.
    """
    mapped = _with_provenance_translate_map(
        connection, physical_schema=schema_if_supported(SCHEMA_REGISTRY_SCHEMA, connection)
    )
    rows = mapped.execute(sa.select(SchemaRegistry).order_by(SchemaRegistry.id)).mappings().all()
    kept: dict[tuple[str, str], Any] = {}
    warnings: list[str] = []
    for row in rows:
        key = (row["database_config_name"], row["schema_tag"])
        current = current_by_key.get(key)
        if key not in kept:
            kept[key] = row
        elif row["physical_schema"] != kept[key]["physical_schema"]:
            warnings.append(
                f"registry: {key[0]!r} {key[1]!r} had conflicting rows "
                f"({kept[key]['physical_schema']!r} vs {row['physical_schema']!r}); "
                f"kept {current if current is not None else kept[key]['physical_schema']!r}."
            )
            if row["physical_schema"] == current:
                kept[key] = row
    SchemaRegistry.__table__.drop(bind=mapped)
    mapped = _ensure_schema_registry_table(connection)
    mapped.execute(sa.insert(SchemaRegistry), [dict(row) for row in kept.values()])
    return warnings


def plan_baselines(stack: StackConfig, renames: dict[str, str]) -> Plan:
    """Plan a schema-registry baseline for every populated database and role.

    Parameters
    ----------
    stack : StackConfig
        Validated 2.0 config (renames already applied).
    renames : dict[str, str]
        Entry renames; registry rows owned by the old name move to the new name.
    """
    plan = Plan()
    resolver = Resolver(stack)
    previous_names = {new: old for old, new in renames.items()}
    seen_connections: dict[str, ResolvedConnection] = {}
    wanted: dict[str, dict[tuple[str, str], str | None]] = defaultdict(dict)

    for name in resolver.database_names():
        resolved = resolver.resolve_database(name)
        for tag in (role.value for role in resolved.schema_tags()):
            role = resolved.route_for_schema_tag(tag, vocab=Role.VOCAB, primary=Role.PRIMARY)
            connection = resolved.connection_for_role(role)
            if connection.test_only:
                plan.notes.append(f"baseline: {name} {tag}: test-only connection, skipped.")
                continue
            if not is_openable(connection):
                plan.notes.append(f"baseline: {name} {tag}: database file does not exist, skipped.")
                continue
            seen_connections[connection.physical_key()] = connection
            _plan_one_baseline(plan, resolved, name, tag, connection, wanted[connection.physical_key()], previous_names.get(name))

    for key, connection in seen_connections.items():
        _plan_registry_cleanup(plan, connection, wanted[key], renames)
    return plan


def schema_and_tables(live: sa.Connection, resolved: Any, tag: str) -> tuple[str | None, list[str]]:
    """The schema *tag* resolves to on *live* and the tables it holds."""
    schema = resolved.configured_internal_schema_translate_map().get(tag) or sa.inspect(live).default_schema_name
    tables = sa.inspect(live).get_table_names(schema=schema) if schema or not supports_schemas(live) else []
    return schema, tables


def _plan_one_baseline(
    plan: Plan,
    resolved: Any,
    name: str,
    tag: str,
    connection: ResolvedConnection,
    wanted: dict[tuple[str, str], str | None],
    previous_name: str | None,
) -> None:
    """Append the baseline action for one ``(entry, tag)``, or a skip note or problem.

    A row recorded under *previous_name* (a renamed entry) counts as the existing baseline.
    """
    engine = connection.create_engine()
    try:
        with engine.connect() as live:
            schema, tables = schema_and_tables(live, resolved, tag)
            wanted[(name, tag)] = schema
            layout = registry_layout(live)
            existing = None
            if layout == "current":
                existing = next(
                    (
                        row
                        for row in _list_registry_rows(live)
                        if row.schema_tag == tag and row.database_config_name in (name, previous_name)
                    ),
                    None,
                )
    except SQLAlchemyError as exc:
        plan.problems.append(f"baseline: {name} {tag}: cannot read {connection.safe_url}: {type(exc).__name__}")
        return
    finally:
        engine.dispose()

    if not tables:
        plan.notes.append(f"baseline: {name} {tag}: schema {schema!r} has no tables, skipped.")
        return
    if existing is not None:
        if existing.physical_schema != schema:
            plan.problems.append(
                f"baseline: {name} {tag}: registry records {existing.physical_schema!r} but the config "
                f"resolves {schema!r}. Run `omop-config acknowledge-schema-migration` if that is deliberate."
            )
        return

    def run(connection: ResolvedConnection = connection, name: str = name, tag: str = tag, schema: str | None = schema) -> None:
        engine = connection.create_engine()
        try:
            with engine.begin() as live:
                _record_schema_provenance(
                    live,
                    database_config_name=name,
                    schema_tag=tag,
                    new_physical_schema=schema,
                    reason=Names.REASON,
                )
        finally:
            engine.dispose()

    plan.actions.append(Action(f"baseline: {name} {tag} -> {schema or '(default)'} ({len(tables)} table(s))", run))


def _plan_registry_cleanup(
    plan: Plan,
    connection: ResolvedConnection,
    wanted: dict[tuple[str, str], str | None],
    renames: dict[str, str],
) -> None:
    """Plan the pre-release layout rebuild and registry-row renames on one database."""
    engine = connection.create_engine()
    try:
        with engine.connect() as live:
            layout = registry_layout(live)
            if layout == "unknown":
                plan.problems.append(f"registry: {connection.safe_url} has an unrecognised schema_registry layout.")
                return
            if layout == "absent":
                return
            old_rows = _list_registry_rows(live) if layout == "current" and renames else ()
    except SQLAlchemyError as exc:
        plan.problems.append(f"registry: cannot read {connection.safe_url}: {type(exc).__name__}")
        return
    finally:
        engine.dispose()

    renamed = [(row.database_config_name, row.schema_tag) for row in old_rows if row.database_config_name in renames]
    clashes = {(renames[o], t) for o, t in renamed} & {(r.database_config_name, r.schema_tag) for r in old_rows}
    if clashes:
        plan.problems.append(f"registry: renaming would duplicate existing rows {sorted(clashes)} on {connection.safe_url}.")
        return

    def run(connection: ResolvedConnection = connection, layout: str = layout) -> None:
        engine = connection.create_engine()
        try:
            with engine.begin() as live:
                if layout == "connection_key":
                    for warning in rebuild_registry(live, wanted):
                        console.print(f"  [yellow]{escape(warning)}[/yellow]")
                registry = _registry_connection(live)
                for old, new in renames.items():
                    registry.execute(
                        sa.update(SchemaRegistry)
                        .where(SchemaRegistry.database_config_name == old)
                        .values(database_config_name=new)
                    )
        finally:
            engine.dispose()

    if layout == "connection_key":
        plan.actions.insert(0, Action(f"registry: rebuild {connection.safe_url} schema_registry in the 2.0 layout", run))
    elif renamed:
        plan.actions.insert(0, Action(f"registry: rename {len(renamed)} row(s) on {connection.safe_url}", run))


# --------------------------------------------------------------------------
# Step 3: omop-emb embedding registries
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyRegistry:
    """A pre-per-store ``model_registry`` found in one schema.

    Parameters
    ----------
    schema : str or None
        Schema holding the table; None on SQLite.
    table : str
        ``model_registry`` or, after a partial run, ``model_registry_legacy_v1``.
    models : tuple[str, ...]
        Model names stored in the table.
    """

    schema: str | None
    table: str
    models: tuple[str, ...]


def schemas_holding(live: sa.Connection, table: str) -> list[str | None]:
    """Schemas that hold *table*, the registry schema first; ``[None]`` where the dialect has no schemas."""
    inspector = sa.inspect(live)
    if not supports_schemas(live):
        return [None] if inspector.has_table(table) else []
    own = [MODEL_REGISTRY_SCHEMA] if inspector.has_table(table, schema=MODEL_REGISTRY_SCHEMA) else []
    return [*own, *find_table_in_other_schemas(live, table, physical_schema=MODEL_REGISTRY_SCHEMA)]


def find_legacy_registries(live: sa.Connection) -> tuple[list[LegacyRegistry], list[str]]:
    """Legacy-layout registries in every schema, and rows with a NULL provider.

    Returns
    -------
    tuple[list[LegacyRegistry], list[str]]
        The registries and a problem line per table holding a NULL ``provider_type``.
    """
    inspector = sa.inspect(live)
    found: list[LegacyRegistry] = []
    problems: list[str] = []
    for table in (Names.REGISTRY_TABLE, Names.LEGACY_TABLE):
        for schema in schemas_holding(live, table):
            if not _is_legacy_layout(columns_of(inspector, table, schema)):
                continue
            qualified_name = qualified(live, table, physical_schema=schema)
            models = tuple(row[0] for row in live.execute(sa.text(f"SELECT model_name FROM {qualified_name}")))
            nulls = live.execute(sa.text(f"SELECT count(*) FROM {qualified_name} WHERE provider_type IS NULL")).scalar_one()
            if nulls:
                problems.append(f"{qualified_name} has {nulls} row(s) with NULL provider_type; 2.0 requires one.")
            found.append(LegacyRegistry(schema, table, models))
    for schema in {registry.schema for registry in found}:
        tables = {registry.table for registry in found if registry.schema == schema}
        if len(tables) == 2:
            problems.append(
                f"schema {schema!r} holds both {Names.REGISTRY_TABLE} and {Names.LEGACY_TABLE}; "
                "resolve which one is current by hand."
            )
    return found, problems


def current_registry_rows(live: sa.Connection) -> list[tuple[str, str]]:
    """``(database_config_name, model_name)`` rows of the 2.0 registry, or none if absent."""
    schema = schema_if_supported(MODEL_REGISTRY_SCHEMA, live)
    columns = columns_of(sa.inspect(live), Names.REGISTRY_TABLE, schema)
    if columns is None or "database_config_name" not in columns:
        return []
    table = qualified(live, Names.REGISTRY_TABLE, physical_schema=schema)
    return [(r[0], r[1]) for r in live.execute(sa.text(f"SELECT database_config_name, model_name FROM {table}"))]


def plan_embedding_registries(stack: StackConfig, renames: dict[str, str]) -> Plan:
    """Plan the migration of every legacy omop-emb registry reachable from a vector store.

    A 2.0 config allows one vector store per physical database, so each
    backing entry is planned on its own. Registry rows owned by a renamed
    entry move to its new name.
    """
    plan = Plan()
    resolver = Resolver(stack)
    seen: set[str] = set()
    for store_name in resolver.vector_store_names():
        entry = resolver.resolve_vector_store(store_name).database
        if entry.name not in seen:
            seen.add(entry.name)
            _plan_entry_registry(plan, entry, {new: old for old, new in renames.items()}.get(entry.name))
    return plan


def _plan_entry_registry(plan: Plan, entry: Any, previous_name: str | None) -> None:
    """Plan the legacy registry owned by *entry*, or record why it cannot be migrated.

    Stops the run for a legacy registry in a schema *entry* does not own, a
    NULL ``provider_type``, or a model held by more than one registry.
    """
    connection = entry.connection_for_role(Role.PRIMARY)
    label = f"emb: {entry.name} ({connection.safe_url})"
    if connection.dialect_name not in Names.SUPPORTED_DIALECTS:
        plan.problems.append(f"{label}: dialect {connection.dialect_name!r} is not supported by the migration.")
        return
    if not is_openable(connection):
        plan.notes.append(f"{label}: database file does not exist, skipped.")
        return

    engine = connection.create_engine()
    try:
        with engine.connect() as live:
            legacy, problems = find_legacy_registries(live)
            current = current_registry_rows(live)
            owned_schema = entry.schema_name or sa.inspect(live).default_schema_name if supports_schemas(live) else None
    except SQLAlchemyError as exc:
        plan.problems.append(f"{label}: cannot read the database: {type(exc).__name__}")
        return
    finally:
        engine.dispose()

    renamed_rows = [model for owner, model in current if previous_name is not None and owner == previous_name]
    current = [(entry.name if owner == previous_name else owner, model) for owner, model in current]
    migrated_models = {model for _, model in current}
    todo = [r for r in legacy if not (r.table == Names.LEGACY_TABLE and set(r.models) <= migrated_models)]
    owned = [r for r in todo if r.schema == owned_schema]
    problems = [f"{label}: {line}" for line in problems]
    problems += [
        f"{label}: legacy registry {qualified_label(r)} is not in this entry's schema ({owned_schema!r}); "
        "move or drop it by hand."
        for r in todo
        if r.schema != owned_schema
    ]
    holders: dict[str, set[str]] = defaultdict(set)
    for owner, model in current:
        holders[model].add(owner)
    for registry in todo:
        for model in registry.models:
            holders[model].add(entry.name if registry.schema == owned_schema else qualified_label(registry))
    problems += [
        f"{label}: model {model!r} is held by several registries ({', '.join(sorted(who))}); "
        "2.0 allows one per database."
        for model, who in sorted(holders.items())
        if len(who) > 1
    ]
    if problems:
        plan.problems += problems
        return

    if renamed_rows:
        plan.actions.append(
            Action(
                f"emb: {entry.name}: rename {len(renamed_rows)} registry row(s) from {previous_name!r}",
                lambda: rename_registry_rows(connection, previous_name, entry.name),
            )
        )
    for registry in owned:
        target = MODEL_REGISTRY_SCHEMA if supports_schemas(connection.dialect_name) else Names.REGISTRY_TABLE
        plan.actions.append(
            Action(
                f"emb: {entry.name}: migrate {qualified_label(registry)} ({len(registry.models)} model(s)) "
                f"to {target}, keep legacy as {Names.LEGACY_TABLE}",
                lambda registry=registry: migrate_registry(entry, registry),
            )
        )


def rename_registry_rows(connection: ResolvedConnection, old: str, new: str) -> None:
    """Move the 2.0 registry rows owned by entry *old* to entry *new*."""
    engine = connection.create_engine()
    try:
        with engine.begin() as live:
            table = qualified(live, Names.REGISTRY_TABLE, physical_schema=schema_if_supported(MODEL_REGISTRY_SCHEMA, live))
            live.execute(
                sa.text(f"UPDATE {table} SET database_config_name = :new WHERE database_config_name = :old"),
                {"new": new, "old": old},
            )
    finally:
        engine.dispose()


def qualified_label(registry: LegacyRegistry) -> str:
    """``schema.table`` (or just the table name) for messages."""
    return f"{registry.schema}.{registry.table}" if registry.schema else registry.table


def migrate_registry(entry: Any, registry: LegacyRegistry) -> None:
    """Move one legacy registry into the 2.0 registry; resumable after a partial run.

    Renames the legacy table, creates the 2.0 registry through omop-emb,
    copies the rows with ``provider_type`` lowercased, and checks the copy.
    Storage identifiers are kept unchanged.

    Parameters
    ----------
    entry : ResolvedDatabase
        Entry that owns the registry; becomes ``database_config_name``.
    registry : LegacyRegistry
        The legacy table to migrate.

    Raises
    ------
    RuntimeError
        If the copied rows do not match the legacy rows.
    """
    connection = entry.connection_for_role(Role.PRIMARY)
    bare = connection.create_engine()
    try:
        if registry.table == Names.REGISTRY_TABLE:
            with bare.begin() as live:
                source = qualified(live, Names.REGISTRY_TABLE, physical_schema=registry.schema)
                live.execute(sa.text(f"ALTER TABLE {source} RENAME TO {live.dialect.identifier_preparer.quote(Names.LEGACY_TABLE)}"))
    finally:
        bare.dispose()

    writer = registry_writer_engine(entry)
    try:
        with writer.begin() as live:
            target = qualified(live, Names.REGISTRY_TABLE, physical_schema=resolve_registry_physical_schema(live))
            source = qualified(live, Names.LEGACY_TABLE, physical_schema=registry.schema)
            columns = [c.name for c in ModelRegistry.__table__.columns if c.name != "database_config_name"]
            select = ", ".join("lower(provider_type)" if c == "provider_type" else c for c in columns)
            live.execute(
                sa.text(
                    f"INSERT INTO {target} (database_config_name, {', '.join(columns)}) "
                    f"SELECT CAST(:entry AS VARCHAR), {select} FROM {source} src "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {target} n "
                    "WHERE n.database_config_name = :entry AND n.model_name = src.model_name)"
                ),
                {"entry": entry.name},
            )
            expected = live.execute(sa.text(f"SELECT count(*) FROM {source}")).scalar_one()
            copied = live.execute(
                sa.text(
                    f"SELECT count(*) FROM {source} src JOIN {target} n "
                    "ON n.database_config_name = :entry AND n.model_name = src.model_name "
                    "AND n.storage_identifier = src.storage_identifier"
                ),
                {"entry": entry.name},
            ).scalar_one()
            if copied != expected:
                raise RuntimeError(
                    f"{entry.name}: copied {copied} of {expected} registry rows; "
                    f"nothing was lost, {Names.LEGACY_TABLE} is intact. Fix and re-run."
                )
    finally:
        writer.dispose()


# --------------------------------------------------------------------------
# Step 4: verify
# --------------------------------------------------------------------------


def verify(stack: StackConfig) -> bool:
    """Open every CDM engine and vector-store registry read-only and print OK or FAIL.

    Returns
    -------
    bool
        True when every item passed.
    """
    from oa_configurator.cli import _verify_schema_provenance

    resolver = Resolver(stack)
    table = Table("item", "tag", "", "status", "detail")
    ok = True
    for name in resolver.database_names():
        resolved = resolver.resolve_database(name)
        if not all(is_openable(c) and not c.test_only for c in connections_of(resolved)):
            continue
        for tag in (role.value for role in resolved.schema_tags()):
            role = resolved.route_for_schema_tag(tag, vocab=Role.VOCAB, primary=Role.PRIMARY)
            engine = resolved.connection_for_role(role).create_engine()
            try:
                with engine.connect() as live:
                    populated = bool(schema_and_tables(live, resolved, tag)[1])
            finally:
                engine.dispose()
            if populated:
                ok &= _verify_schema_provenance(table, resolved, tag, label=name)
            else:
                table.add_row(name, tag, "-", "[yellow]SKIP[/yellow]", "no tables")
    for store_name in resolver.vector_store_names():
        database = resolver.resolve_vector_store(store_name).database
        if not is_openable(database.connection_for_role(Role.PRIMARY)):
            table.add_row(store_name, "registry", "-", "[yellow]SKIP[/yellow]", "database file missing")
            continue
        try:
            engine = registry_reader_engine(database)
            try:
                with engine.connect() as live:
                    schema = schema_if_supported(MODEL_REGISTRY_SCHEMA, live)
                    present = sa.inspect(live).has_table(Names.REGISTRY_TABLE, schema=schema)
                    count = (
                        live.execute(sa.text(f"SELECT count(*) FROM {qualified(live, Names.REGISTRY_TABLE, physical_schema=schema)}")).scalar_one()
                        if present
                        else 0
                    )
            finally:
                engine.dispose()
        except Exception as exc:  # noqa: BLE001 - report every failure per store
            table.add_row(store_name, "registry", "?", "[red]FAIL[/red]", f"{type(exc).__name__}: {str(exc)[:70]}")
            ok = False
        else:
            table.add_row(store_name, "registry", "-", "[green]OK[/green]", f"{count} model(s)")
    console.print(table)
    return ok


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def backup_files(config_path: Path, plan_connections: list[ResolvedConnection], backup_dir: Path) -> list[Path]:
    """Copy the config and every SQLite file the run may change into *backup_dir*."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    copies = []
    target = backup_dir / f"{config_path.name}.{stamp}.bak"
    os.replace(_copy_secure_temp(config_path, target), target)
    copies.append(target)
    for path in dict.fromkeys(filter(None, map(sqlite_file, plan_connections))):
        if not path.exists():
            continue
        target = backup_dir / f"{path.name}.{stamp}.bak"
        with sqlite3.connect(path) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        copies.append(target)
    return copies


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=active_config_path(), help="config.toml to migrate")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    parser.add_argument(
        "--rename",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="rename a [databases.*] entry; repeatable",
    )
    parser.add_argument("--backup-dir", type=Path, default=None, help="where backups go (default: next to the config)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the migration. Exit code 0 on success, 1 when verification fails, 2 on usage or blocking problems."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    config_path: Path = args.config.expanduser().resolve()
    if not config_path.exists():
        console.print(f"[red]Config file not found:[/red] {config_path}")
        return 2
    try:
        renames = dict(item.split("=", 1) for item in args.rename)
    except ValueError:
        console.print("[red]--rename expects OLD=NEW[/red]")
        return 2

    original = config_path.read_text(encoding="utf-8")
    new_text, stack, plan = plan_config(original, renames, config_path)
    if stack is not None and not plan.problems:
        plan.extend(plan_baselines(stack, renames))
        plan.extend(plan_embedding_registries(stack, renames))

    mode = "APPLY" if args.apply else "DRY RUN"
    console.print(f"[bold]{mode}[/bold] {config_path}")
    for note in plan.notes:
        console.print(f"  {escape(note)}")
    for action in plan.actions:
        console.print(f"  [cyan]{escape(action.label)}[/cyan]")
    config_changed = new_text != original
    if config_changed:
        console.print("  [cyan]config: write updated config.toml[/cyan]")
    for problem in plan.problems:
        console.print(f"  [red]STOP {escape(problem)}[/red]")
    if plan.problems:
        console.print("[red]Nothing was changed.[/red]")
        return 2
    if not plan.actions and not config_changed:
        console.print("Nothing to do.")
        return 0 if stack is None or verify(stack) else 1
    if not args.apply:
        console.print("Dry run only. Re-run with --apply to make these changes.")
        return 0

    resolver = Resolver(stack)
    connections = [
        connection
        for name in resolver.database_names()
        for connection in connections_of(resolver.resolve_database(name))
    ]
    backup_dir = args.backup_dir or config_path.parent
    for copy in backup_files(config_path, connections, backup_dir):
        console.print(f"  backup: {copy}")
    for action in plan.actions:
        action.run()
        console.print(f"  done: {escape(action.label)}")
    if config_changed:
        os.replace(_write_secure_temp(config_path, new_text.encode("utf-8")), config_path)
        _fsync_directory(config_path.parent)
        console.print("  done: config.toml updated")
    return 0 if verify(stack) else 1


if __name__ == "__main__":
    raise SystemExit(main())
