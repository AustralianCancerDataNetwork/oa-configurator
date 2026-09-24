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

The SQLAlchemy backend names (`Engine.dialect.name` / `get_backend_name()`) this codebase recognizes: currently `postgresql` and `sqlite`. Distinct from `ConnectionConfig.dialect`, which stays a free-form string to carry a driver suffix such as `postgresql+psycopg`; `Dialect` is the plain backend-name axis every dialect-keyed dispatch in this codebase branches on. See [Dialect support across the stack](../architecture.md#dialect-support-across-the-stack) for the convention every consuming repo follows to add a new one.

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

::: oa_configurator.domains.resources.sql.validate_schema_tag

::: oa_configurator.domains.resources.sql.register_reserved_schema

::: oa_configurator.domains.resources.sql.register_reserved_schema_tag

::: oa_configurator.domains.resources.sql.registered_schema_tags

## Schema provenance

See [Schema provenance guard](../architecture.md#schema-provenance-guard)
for the full explanation of what this guards against and when it no-ops.

::: oa_configurator.domains.resources.schema.guard_schema_provenance_for

::: oa_configurator.domains.resources.sql.record_schema_provenance

::: oa_configurator.domains.resources.sql.find_table_in_other_schemas

::: oa_configurator.domains.resources.sql.SchemaDriftError
