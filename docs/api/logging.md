# Logging API

`RedactingFilter` is what `configure_logging()` actually installs. `RedactingFormatter`
is deprecated in favor of it (see [Secrets](../secrets.md)).

::: oa_configurator.logging_config
    options:
      members:
        - LoggingConfig
        - RedactingFilter
        - RedactingFormatter
        - configure_logging
        - get_logger
