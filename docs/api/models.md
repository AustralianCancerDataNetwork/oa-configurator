# Models

Typed Pydantic models that represent the human-managed configuration structure. All models are exported from the top-level `oa_configurator` package.

## StackConfig

Root configuration object. Also the entry point for programmatic construction via `StackConfig.for_session()`.

::: oa_configurator.models.StackConfig

## DatabaseConfig

::: oa_configurator.models.DatabaseConfig

## Resource models

### ResourceKind

::: oa_configurator.models.ResourceKind

### ResourceConfigBase

::: oa_configurator.models.ResourceConfigBase

### CDMResourceConfig

::: oa_configurator.models.CDMResourceConfig

### EmbeddingResourceConfig

::: oa_configurator.models.EmbeddingResourceConfig

## Knowledge resource models

### KnowledgeResourceKind

::: oa_configurator.models.KnowledgeResourceKind

### KnowledgeResourceConfig

::: oa_configurator.models.KnowledgeResourceConfig

### LocalPathKnowledgeResource

::: oa_configurator.models.LocalPathKnowledgeResource

## ToolConfig

::: oa_configurator.models.ToolConfig

## ProfileOverrideConfig

::: oa_configurator.models.ProfileOverrideConfig
