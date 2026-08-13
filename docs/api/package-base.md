# PackageConfigBase API

`StackConfig.tools` deliberately stores untyped dictionaries because consuming
packages are discovered at runtime. `PackageConfigBase.validate_candidate()` is
the package-aware boundary: it constructs the concrete package model, runs all
field and model validators, and validates each `RefTo` against the same candidate
stack without reading or writing a file.

Schema failures raise `PackageConfigValidationError`. Its `tool_name` identifies
the `[tools.<name>]` section, while `errors()` preserves pydantic locations,
including nested paths and the empty location used by cross-field validators.
Reference failures raise `ConfigurationError` with the package field path.

::: oa_configurator.package_base.PackageConfigBase

::: oa_configurator.package_base.PackageConfigValidationError
