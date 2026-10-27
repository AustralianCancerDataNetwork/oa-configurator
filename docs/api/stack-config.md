# Stack Config

`StackConfig` is the root model for `config.toml`. It holds every named connection, database, provider, model, vector store, and per-package tool section in one object, and validates cross-references between them at construction time.

::: oa_configurator.stack_config.StackConfig

## Cross-reference validation

Every reference between sections (a database naming its connection, a model naming its provider, a vector store naming its database, a package's own field naming any of the above) uses the same generic marker instead of a hand-written validator per pair.

::: oa_configurator.references.RefTo

`RefTo` names an entry class, and that class says which section holds it. Entry classes derive from `Referenceable`, which carries the section name, so there is no separate table to keep in sync. A class that is not `Referenceable`, such as the abstract `DatabaseConfig`, is rejected as a `RefTo` target with `UnknownRefTarget`.

::: oa_configurator.references.Referenceable

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

## Cloning and deleting entries

`clone_entry()` returns a validated copy of the stack with one entry copied under a new name. Nothing is repointed: the source keeps its references and the copy points at the same targets until you override them. Many entries cannot sit next to an unchanged copy (two CDM databases on one connection, two identical connections), so `overrides` names the fields to change, keyed by the entry's own field names. A test database on its own connection is two calls: clone the connection with a new `database_name` and `test_only`, then clone the database with `connection` set to it.

`delete_entry()` returns a validated copy without an entry, but only when nothing references it. It refuses and lists the referencing fields, and it refuses when any tool section has no known package class, since that section might hold a reference. There is no way to force it.

`remove_tool_section()` returns a copy without one `[tools.*]` section. Nothing refers to a tool section, so it needs no package class and works for a package that is broken or no longer installed.

All three are pure like `rename_entry()`: no file I/O, input unchanged, and an exception instead of a partial result.

::: oa_configurator.references.clone_entry

::: oa_configurator.references.delete_entry

::: oa_configurator.references.remove_tool_section

The CLI wraps them:

- `omop-config <section> clone NAME NEW_NAME` copies the entry `NAME` to a new entry called `NEW_NAME` in the same section. For example, `omop-config connections clone prod test` creates `[connections.test]` from `[connections.prod]`. Flags change fields of the copy. Without flags every field is prompted, with the source's value as the default, and a reference can point at a new entry that is created in the same flow. That new entry starts from the one the source currently points at, so only the fields that differ need changing. `databases` has one subcommand per kind, `databases clone cdm` and `databases clone generic`, and refuses a source of the other kind.
- `omop-config <section> delete NAME` confirms before deleting, or skips the prompt with `--yes`. When a tool section's package is not usable it prints why.
- `omop-config tools list` shows each tool section and whether its package is available. `omop-config tools remove NAME` removes a section, with the same confirmation.
