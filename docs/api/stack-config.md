# Stack Config

`StackConfig` is the root model for `config.toml`. It holds every named connection, database, provider, model, vector store, and per-package tool section in one object, and validates cross-references between them at construction time.

::: oa_configurator.stack_config.StackConfig

## Cross-reference validation

Every reference between sections (a database naming its connection, a model naming its provider, a vector store naming its database, a package's own field naming any of the above) uses the same generic marker instead of a hand-written validator per pair.

::: oa_configurator.refs.RefTo

::: oa_configurator.references.unresolved_refs

## Kind mismatches

A `RefTo` names *which section* an entry must live in; it doesn't narrow *which subtype*. `mismatched_kind_refs()` covers the one place that matters today: a `RefTo(GenericDatabaseConfig)` field (e.g. `VectorStoreConfig.database`) pointed at a `CDMDatabaseConfig` entry, or a `RefTo(CDMDatabaseConfig)` field pointed at a `GenericDatabaseConfig` one. Reads as a distinct "wrong kind" error rather than `unresolved_refs()`'s "doesn't exist" one, and runs alongside it at the same three validation sites (`StackConfig.validate_references`, the CLI's entry-reference check, `Resolver.resolve_package_config()`).

::: oa_configurator.references.mismatched_kind_refs

Application code can import both helpers from the public package surface: `from oa_configurator import mismatched_kind_refs, unresolved_refs`.

## Finding what uses an entry

`references_to()` answers the reverse question: which entries and package fields name a given connection, database, provider, model or vector store. Use it before changing a shared target. It reads the same `RefTo` markers as the forward helpers, and returns a `ReferenceReport`.

`[tools.*]` values are untyped, so `references_to()` needs the package classes to find their `RefTo` fields. Pass them as `package_configs`, a mapping of `tool_name` to class. A tool section with no known class is listed in `unknown_tool_schemas` instead of being read as holding no references. `find_references()` discovers the classes from the `omop.config` entry points and also reports packages that failed to import or are registered twice in `discovery_problems`.

::: oa_configurator.references.references_to

::: oa_configurator.references.find_references

::: oa_configurator.references.ReferenceReport

::: oa_configurator.references.Reference

::: oa_configurator.references.DiscoveryProblem

## Renaming an entry

`rename_entry()` returns a validated copy of the stack with one entry renamed and every reference to it repointed, including `[tools.*]` fields that referred to it through their default. It does no file I/O and leaves its input unchanged. It raises instead of returning a partial result, and refuses when any tool section has no known package class. Save the result with `save_stack_config()`.

::: oa_configurator.references.rename_entry
