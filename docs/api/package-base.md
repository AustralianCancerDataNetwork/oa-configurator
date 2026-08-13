# PackageConfigBase API

`StackConfig.tools` deliberately stores untyped dictionaries because consuming
packages are discovered at runtime. `PackageConfigBase.validate_candidate()` is
the package-aware boundary: it constructs the concrete package model, runs all
field and model validators, and validates each `RefTo` against the same candidate
stack without reading or writing a file.

Schema failures raise `PackageConfigValidationError`. Its `tool_name` identifies
the `[tools.<name>]` section, while `errors()` preserves pydantic locations and
messages, including nested paths and the empty location used by cross-field
validators. Rejected input and validator context are omitted so exception text,
reprs, and CLI rendering cannot echo secrets. Reference failures raise
`ConfigurationError` with the package field path.

`plan_configure()` is the headless write-planning boundary. It deep-copies a
stack, resolves non-interactive values—including nested `RefTo` creation or
updates—validates the concrete package section and complete stack, and returns
the new candidate. It performs no loader or persistence calls and leaves the
input unchanged on both success and failure. A bound `loaded_path` is preserved
on the returned candidate as source provenance. Planning failures raise
`ConfigurationError` or `PackageConfigValidationError`; the headless API never
prints CLI guidance or raises `typer.Exit`.

::: oa_configurator.package_base.PackageConfigBase

::: oa_configurator.package_base.PackageConfigValidationError

::: oa_configurator.package_base.plan_configure
