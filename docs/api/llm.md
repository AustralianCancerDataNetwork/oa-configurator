# LLM

Provider connections and named, concretely-configured models for LLM/embedding backends. Peer of the [Resources](resources.md) domain: `ProviderConfig` plays the same role as `ConnectionConfig`, and `ModelConfig` plays the same role as `DatabaseConfig`.

## ProviderConfig

A concrete LLM/embedding provider connection: provider key, base URL, API key. Stored in `[providers.<name>]`.

::: oa_configurator.domains.llm.schema.ProviderConfig

## ModelConfig

A named, reusable, concretely-configured model, served through a provider. Stored in `[models.<name>]`.

::: oa_configurator.domains.llm.schema.ModelConfig

## Resolved types

`ProviderConfig.resolve()` and `ModelConfig.resolve()` produce these. `Resolver.resolve_provider()`/`resolve_model()` are thin wrappers around the same two methods.

::: oa_configurator.domains.llm.schema.ResolvedProvider

::: oa_configurator.domains.llm.schema.ResolvedModel
