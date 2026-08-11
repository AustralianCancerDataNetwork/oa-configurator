# Resources

Physical connections, and the databases built on top of them. A database is one of two kinds, discriminated by a required `kind` field: a plain generic database, or a CDM database with its logical vocab/results role bundle.

## ConnectionConfig

A concrete database endpoint: dialect, host, credentials, target database. Stored in `[connections.<name>]`.

::: oa_configurator.domains.resources.schema.ConnectionConfig

## DatabaseKind

::: oa_configurator.domains.resources.schema.DatabaseKind

## DatabaseConfig

The shared base: `kind`, `connection`, `schema_name`. Not constructed directly — every `[databases.<name>]` entry is one of the two concrete kinds below, chosen by its own `kind` field. `schema_name` defaults differently per kind: unset on `GenericDatabaseConfig` means "no schema override, use the connection's own default"; `CDMDatabaseConfig` defaults it to `"omop"`.

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

## Resolved types

`ConnectionConfig.resolve()`, `GenericDatabaseConfig.resolve()`, and `CDMDatabaseConfig.resolve()` produce these. `Resolver.resolve_connection()`/`resolve_database()` are thin wrappers around the same methods; `resolve_database()` returns the resolved subtype matching the entry's own `kind`.

::: oa_configurator.domains.resources.schema.ResolvedConnection

::: oa_configurator.domains.resources.schema.ResolvedDatabase

::: oa_configurator.domains.resources.schema.ResolvedCDMDatabase
