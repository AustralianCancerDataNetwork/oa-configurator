# Resolver

The `Resolver` turns logical names in a `StackConfig` into concrete typed handles with secrets resolved and paths expanded.

## Resolver

::: oa_configurator.resolver.Resolver

## Resolved resource types

`resolve_resource()` returns a `ResolvedResourceBase` subclass. Use `isinstance` to narrow to a specific kind.

### ResolvedResourceBase

::: oa_configurator.resolver.ResolvedResourceBase

### ResolvedCDMResource

::: oa_configurator.resolver.ResolvedCDMResource

### ResolvedEmbeddingResource

::: oa_configurator.resolver.ResolvedEmbeddingResource

## Resolved knowledge resource types

### ResolvedKnowledgeResource

::: oa_configurator.resolver.ResolvedKnowledgeResource

### ResolvedLocalPathKnowledgeResource

::: oa_configurator.resolver.ResolvedLocalPathKnowledgeResource

## ResolvedToolConfig

::: oa_configurator.resolver.ResolvedToolConfig
