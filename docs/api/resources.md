# Resources

Physical connections and the logical CDM/vocab/results database bundles built on top of them.

## ConnectionConfig

A concrete database endpoint: dialect, host, credentials, target database. Stored in `[connections.<name>]`.

::: oa_configurator.domains.resources.schema.ConnectionConfig

## DatabaseConfig

Maps the OMOP logical roles (CDM, vocab, results) to named connections and schema names. Stored in `[databases.<name>]`.

::: oa_configurator.domains.resources.schema.DatabaseConfig

## Role

::: oa_configurator.domains.resources.schema.Role

## Resolved types

`ConnectionConfig.resolve()` and `DatabaseConfig.resolve()` produce these. `Resolver.resolve_connection()`/`resolve_database()` are thin wrappers around the same two methods.

::: oa_configurator.domains.resources.schema.ResolvedConnection

::: oa_configurator.domains.resources.schema.ResolvedDatabase
