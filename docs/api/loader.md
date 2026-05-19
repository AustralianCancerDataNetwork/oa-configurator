# Loader

The loader reads a TOML file, validates it against the typed models, and applies any environment-variable overrides before returning a bound `StackConfig`.

::: oa_configurator.loader
    options:
      members:
        - load_stack_config
