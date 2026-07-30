# Models

Typed Pydantic models that represent the human-managed configuration structure. All models are exported from the top-level `oa_configurator` package.

## StackConfig

Root configuration object. Also the entry point for programmatic construction via `StackConfig.for_session()`.

::: oa_configurator.models.StackConfig

## DatabaseConfig

::: oa_configurator.models.DatabaseConfig

## ResourceConfig

::: oa_configurator.models.ResourceConfig

## ProviderConfig

::: oa_configurator.models.ProviderConfig

## ModelConfig

::: oa_configurator.models.ModelConfig

## ProfileOverrideConfig

::: oa_configurator.models.ProfileOverrideConfig

## Tool sections

`[tools.<name>]` sections have no dedicated model: `StackConfig.tools` is a plain `dict[str, dict[str, Any]]`, since each package's own schema is only known lazily, via its `PackageConfigBase` subclass (see [PackageConfigBase](package-base.md)), not by `oa-configurator` itself at parse time.
