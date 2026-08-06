# Resolver

The `Resolver` turns logical names in a `StackConfig` into concrete typed handles with secrets resolved and paths expanded.

It is thin dispatch: each `resolve_*` method looks up the raw config entry and delegates to that entry's own `resolve()` method. The resolved types themselves (`ResolvedConnection`, `ResolvedDatabase`/`ResolvedCDMDatabase`, `ResolvedProvider`, `ResolvedModel`, `ResolvedVectorStore`) are documented next to their raw counterpart, under [Resources](resources.md), [LLM](llm.md), and [Vector Stores](vector-stores.md).

## Resolver

::: oa_configurator.resolver.Resolver

## ResolvedToolConfig

The one resolved type that stays here rather than moving to a domain: a raw, untyped `[tools.<name>]` section, returned by `resolve_tool()`. Prefer `resolve_package_config()` (or `PackageConfigBase.get_config()`) for the typed, validated equivalent.

::: oa_configurator.resolver.ResolvedToolConfig
