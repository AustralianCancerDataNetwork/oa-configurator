# PackageConfigBase API

Use these APIs when your application collects package settings itself instead of sending the user through `omop-config configure`.

## Validate a complete candidate

`StackConfig.tools` holds plain dictionaries because oa-configurator cannot know the schemas of packages that are discovered later at runtime. Before accepting a proposed stack, call `PackageConfigBase.validate_candidate()` on the package class. It applies that package's field and model validators and checks every `RefTo` against the same proposed stack, without loading or saving a file.

A schema problem raises `PackageConfigValidationError`. Use `tool_name` to identify the affected `[tools.<name>]` section and `errors()` to attach messages to form fields, including nested fields and model-level errors. These details deliberately omit rejected values and validator context, and the exception does not retain the original pydantic error, so displaying or logging the exception cannot echo a submitted secret. A missing or wrong-kind reference raises `ConfigurationError` with the package field path.

## Plan a change for review

Use `plan_configure()` when your application needs to preview a change before the user approves it. Pass the current stack and the proposed package values; the function returns a new, fully validated `StackConfig` and leaves the current object unchanged, even when planning fails. Nested dictionaries can create or update entries reached through `RefTo` fields, which lets a UI submit one complete proposal instead of reproducing oa-configurator's schema traversal.

Planning never reads or writes the active configuration file. The returned candidate keeps `loaded_path` as provenance, but your application still decides whether and when to call `save_stack_config()`. Failures are ordinary `ConfigurationError` or `PackageConfigValidationError` exceptions: this API does not print CLI guidance or raise `typer.Exit`.

::: oa_configurator.package_base.PackageConfigBase

::: oa_configurator.package_base.PackageConfigValidationError

::: oa_configurator.package_base.plan_configure
