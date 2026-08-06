# Stack Config

`StackConfig` is the root model for `config.toml`. It holds every named connection, database, provider, model, vector store, and per-package tool section in one object, and validates cross-references between them at construction time.

::: oa_configurator.stack_config.StackConfig

## Cross-reference validation

Every reference between sections (a database naming its connection, a model naming its provider, a vector store naming its database, a package's own field naming any of the above) uses the same generic marker instead of a hand-written validator per pair.

::: oa_configurator.refs.RefTo

::: oa_configurator.refs.Sensitive

::: oa_configurator.stack_config.unresolved_refs

## Kind mismatches

A `RefTo` names *which section* an entry must live in; it doesn't narrow *which subtype*. `mismatched_kind_refs()` covers the one place that matters today: a `RefTo(GenericDatabaseConfig)` field (e.g. `VectorStoreConfig.database`) pointed at a `CDMDatabaseConfig` entry, or a `RefTo(CDMDatabaseConfig)` field pointed at a `GenericDatabaseConfig` one. Reads as a distinct "wrong kind" error rather than `unresolved_refs()`'s "doesn't exist" one, and runs alongside it at the same three validation sites (`StackConfig.validate_references`, the CLI's entry-reference check, `Resolver.resolve_package_config()`).

::: oa_configurator.stack_config.mismatched_kind_refs
