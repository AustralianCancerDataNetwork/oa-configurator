# Stack Config

`StackConfig` is the root model for `config.toml`. It holds every named connection, database, provider, model, and per-package tool section in one object, and validates cross-references between them at construction time.

::: oa_configurator.stack_config.StackConfig

## Cross-reference validation

Every reference between sections (a database naming its connection, a model naming its provider, a package's own field naming a database or model) uses the same generic marker instead of a hand-written validator per pair.

::: oa_configurator.refs.RefTo

::: oa_configurator.refs.Sensitive

::: oa_configurator.stack_config.unresolved_refs
