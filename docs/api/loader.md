# Loader

The loader reads a TOML file, validates it against the typed models, and applies any environment-variable overrides before returning a bound `StackConfig`.

Two entry points, differing only in where the path comes from:

| Function | Use when |
|---|---|
| `load_stack_config()` | The application has no config-path option of its own. Reads `CONFIG_PATH` (default `~/.config/omop/config.toml`, overridable with `OA_CONFIG_PATH`). |
| `load_stack_config_from_path(path)` | Something supplies a path — a `--config-path` flag, a CLI command, a test fixture. |

Prefer `load_stack_config_from_path` over reimplementing the read. It warns when the file is group- or world-readable, which matters because the file holds database passwords, and it caches by path and file identity so repeated loads do not re-parse. A hand-rolled `tomllib.loads` + `model_validate` gets neither.

Both raise `ConfigurationError` for a file that is present but unusable: malformed TOML, or content that does not validate. The validation case is a `StackConfigValidationError` naming the offending field paths. Neither ever echoes the value that was rejected, since a rejected value is often the secret itself and these messages end up in issues and CI logs.

::: oa_configurator.loader
    options:
      members:
        - load_stack_config
        - load_stack_config_from_path
        - invalidate_cache
