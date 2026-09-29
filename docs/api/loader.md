# Loader

The loader reads a TOML file, validates it against the typed models, and applies any environment-variable overrides before returning a bound `StackConfig`.

One entry point: `load_stack_config(path)`. Omit `path` and it reads the active config path
(default `~/.config/omop/config.toml`, overridable with `OA_CONFIG_PATH`); pass a path
explicitly when something else supplies one: a `--config-path` flag, a CLI command, a test
fixture. Call `active_config_path()` to read that same default path without loading the file,
e.g. to predict what a no-argument `load_stack_config()` call will read.

Prefer `load_stack_config` over reimplementing the read. It warns when the file is group- or world-readable, which matters because the file holds database passwords, and it caches by path and file identity so repeated loads do not re-parse. A hand-rolled `tomllib.loads` + `model_validate` gets neither.

It raises `ConfigurationError` for a file that is present but unusable: malformed TOML, or content that does not validate. The validation case is specifically a `StackConfigInvalidError` naming the offending field paths. Neither ever echoes the value that was rejected, since a rejected value is often the secret itself and these messages end up in issues and CI logs.

::: oa_configurator.loader
    options:
      members:
        - load_stack_config
        - active_config_path
        - invalidate_cache
