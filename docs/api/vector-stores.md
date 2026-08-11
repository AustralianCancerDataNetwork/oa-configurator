# Vector Stores

Which storage backend an embedding-capable package should use. A third instance of the same two-tier pattern as [Resources](resources.md) (connection/database) and [LLM](llm.md) (provider/model): here there is only one tier, since a vector store just names the `GenericDatabaseConfig` its tables live in plus a backend key, rather than introducing its own leaf-tier section.

## VectorStoreConfig

Stored in `[vector_stores.<name>]`. `backend_type` is a plain string (e.g. `"sqlitevec"`, `"pgvector"`) validated by the owning package, not by `oa-configurator` — the same discipline `ProviderConfig.provider` uses. `database` must reference a `GenericDatabaseConfig` entry, never a `CDMDatabaseConfig` one: a vector store's tables have no CDM vocab/results roles, so a `[databases.*]` entry with `kind = "generic"` is required.

::: oa_configurator.domains.vector_stores.schema.VectorStoreConfig

## Resolved type

`VectorStoreConfig.resolve()` produces this. `Resolver.resolve_vector_store()` is a thin wrapper around the same method.

::: oa_configurator.domains.vector_stores.schema.ResolvedVectorStore
