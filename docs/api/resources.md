# Resources

Physical connections, and the databases built on top of them. A database is one of two kinds, discriminated by a required `kind` field: a plain generic database, or a CDM database with its logical vocab/results role bundle.

## ConnectionConfig

A concrete database endpoint: dialect, host, credentials, target database. Stored in `[connections.<name>]`.

::: oa_configurator.domains.resources.schema.ConnectionConfig

## DatabaseKind

::: oa_configurator.domains.resources.schema.DatabaseKind

## DatabaseConfig

The shared base unifying `kind` and `connection`. Every `[databases.<name>]` entry is one of the two concrete kinds below, chosen by its own `kind` field. Each kind adds its own schema field on top 

- `schema_name` on `GenericDatabaseConfig`, 
- `cdm_schema` on `CDMDatabaseConfig`.

Both default to `None`, meaning "no schema override, use the connection's own default" (Postgres's own `search_path` default, typically `public`), not a guarantee this library makes or encodes in code.

::: oa_configurator.domains.resources.schema.DatabaseConfig

## GenericDatabaseConfig

`kind = "generic"`. A database with no CDM-specific fields: just `connection` and `schema_name`. Used by anything that isn't the CDM itself, e.g. a vector store's own database (see [Vector Stores](vector-stores.md)).

::: oa_configurator.domains.resources.schema.GenericDatabaseConfig

## CDMDatabaseConfig

`kind = "cdm"`. Maps the OMOP logical roles (CDM, vocab, results) to named connections and schema names.

::: oa_configurator.domains.resources.schema.CDMDatabaseConfig

## Role

Selects among a *CDM* database's several connections at resolve time. Not related to `kind`: `kind` decides which fields an entry has at config-authoring time, `Role` selects among one CDM entry's connections. A generic entry only ever has one connection, so `Role` has nothing to select there.

::: oa_configurator.domains.resources.schema.Role

## Dialect

The SQLAlchemy backend names (`Engine.dialect.name` / `get_backend_name()`) this codebase recognizes: `postgresql`, `sqlite`, and `duckdb`. Distinct from `ConnectionConfig.dialect`, which stays a free-form string to carry a driver suffix such as `postgresql+psycopg`; `Dialect` is the plain backend-name axis every dialect-keyed dispatch in this codebase branches on. See [Dialect support across the stack](../architecture.md#dialect-support-across-the-stack) for the convention every consuming repo follows to add a new one.

::: oa_configurator.domains.resources.sql.Dialect

## Resolved types

`ConnectionConfig.resolve()`, `GenericDatabaseConfig.resolve()`, and `CDMDatabaseConfig.resolve()` produce these. `Resolver.resolve_connection()`/`resolve_database()` are thin wrappers around the same methods; `resolve_database()` returns the resolved subtype matching the entry's own `kind`.

::: oa_configurator.domains.resources.schema.ResolvedConnection

::: oa_configurator.domains.resources.schema.ResolvedDatabase

::: oa_configurator.domains.resources.schema.ResolvedCDMDatabase

## Schema-aware SQL primitives

Physical-schema/schema-tag-aware helpers. See
[Schema translate map](../architecture.md#schema-translate-map) for the
distinction between a schema *tag* (an abstract `schema_translate_map` key)
and a *physical* schema (an actual, literal database schema name).

::: oa_configurator.domains.resources.sql.qualified

::: oa_configurator.domains.resources.sql.open_connection

::: oa_configurator.domains.resources.sql.autocommit_connection

::: oa_configurator.domains.resources.sql.ensure_schema

## Foreign-key feasibility and omission reporting

DuckDB can enforce a foreign key within one schema, but not between schemas. PostgreSQL supports both within one database. Neither supports foreign keys between separate physical databases.

For a worked table-creation example, see [Creating tables with unenforced foreign keys](../integration.md#creating-tables-with-unenforced-foreign-keys).

| Task | API |
|---|---|
| Check whether two schema tags can be linked | `resolved.foreign_key_can_span(from_tag, to_tag)` |
| Find keys the database will not enforce | `unenforced_foreign_keys(tables, resolved=resolved)` |
| Prepare table copies for creation | `without_cross_engine_foreign_keys(tables, resolved=resolved)` |

The report is a tuple of `UnenforcedForeignKey` records. Each record contains the original constraint in `.constraint` and a reason in `.reason`:

| Reason | Meaning | What happens during creation |
|---|---|---|
| `cross_database` | Source and target are in separate databases | Removed from the table copies |
| `cross_schema` | The dialect cannot enforce this key between these schemas | Removed from the table copies |
| `deferred` | DuckDB cannot add this key after creation; it is cyclic or uses `use_alter=True` | Kept in the copies; the existing DuckDB compiler skips the later constraint operation |

Pass the complete set of tables being created together to the report and copying functions. Cycle classification depends on that set. The original tables and their ORM join information stay unchanged. Creating the original tables directly does not apply the filtering.

All three APIs accept `physical_schemas`, a mapping from logical tags to physical names. Pass the connection's `schema_translate_map` when using custom tags. `None` means DuckDB's default `main`; if the session uses another default, supply its name explicitly. Unknown DuckDB tags raise `UnregisteredSchemaTagError`. Comparisons ignore ASCII letter case, and the feasibility check opens no connection.

**An empty report is not a general table-creation compatibility check.** For example, DuckDB still rejects `ON DELETE CASCADE`, `SET NULL`, and `SET DEFAULT`. These helpers also do not check existing rows: your loader must validate references for keys the database does not enforce.

::: oa_configurator.domains.resources.schema.UnenforcedForeignKey

::: oa_configurator.domains.resources.schema.unenforced_foreign_keys

::: oa_configurator.domains.resources.schema.without_cross_engine_foreign_keys

## Schema provenance

See [Schema provenance guard](../architecture.md#schema-provenance-guard)
for the full explanation of what this guards against and when it no-ops.

::: oa_configurator.domains.resources.schema.guard_schema_provenance_for

::: oa_configurator.domains.resources.schema_registry.physical_schema_of

::: oa_configurator.domains.resources.schema_registry.find_table_in_other_schemas

::: oa_configurator.domains.resources.schema_registry.SchemaDriftError

::: oa_configurator.domains.resources.schema_registry.SchemaOwnershipError

::: oa_configurator.domains.resources.schema_registry.SchemaRegistryOutdatedError

::: oa_configurator.domains.resources.schema_registry.UnregisteredSchemaTagError
