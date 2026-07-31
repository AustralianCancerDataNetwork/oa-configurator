# oa-configurator

A shared configuration layer for the OMOP-oriented Python stack.

---

**oa-configurator** gives OMOP tools a single typed configuration file instead of a tangle of environment variables and package-local `.env` files.

## Key Concepts
- **Connection**: A concrete database endpoint (host, dialect, credentials)
- **Database**: A logical role bundle, e.g. primary OMOP CDM DB, embedding DB, artifact paths
- **Provider** / **Model**: The same two-tier pattern as Connection/Database, for LLM and embedding backends
- **Tool**: Per-tool settings, e.g. backend, storage roots
- **Logging**: One call configures consistent log output for the entire OMOP Python stack

!!! info
    Configuration lives in one TOML file (default **`~/.config/omop/config.toml`**, overridable via `OA_CONFIG_PATH`) and is loaded once. The Resolver turns logical names into typed, credential-resolved handles ready for use.

## Quick Example

=== "From a config file"

    ```python
    from oa_configurator import load_stack_config, Resolver

    config = load_stack_config()                        # reads CONFIG_PATH (default ~/.config/omop/config.toml)
    resolver = Resolver(config)

    database = resolver.resolve_database("cdm")
    engine   = database.create_engine()                 # SQLAlchemy Engine, schema_translate_map applied
    ```

=== "Inline (no file)"

    ```python
    from oa_configurator import StackConfig, ConnectionConfig, DatabaseConfig, Resolver

    config = StackConfig.for_session(
        connections={"local": ConnectionConfig(dialect="postgresql+psycopg", host="localhost",
                                                database_name="omop", password="omop")},
        databases={"cdm": DatabaseConfig(connection="local", cdm_schema="omop")},
    )
    engine = Resolver(config).resolve_database("cdm").create_engine()
    ```

=== "Session override"

    ```python
    from oa_configurator import load_stack_config, ConnectionConfig, DatabaseConfig, Resolver

    # Load shared team config, redirect one database to a local SQLite connection
    engine = (
        Resolver(load_stack_config())
        .with_overrides(
            connections={"local": ConnectionConfig(dialect="sqlite", database_name="/data/local.db")},
            databases={"cdm": DatabaseConfig(connection="local", cdm_schema="omop")},
        )
        .resolve_database("cdm")
        .create_engine()
    )
    ```

## Next Steps

- [Quick Start](quickstart.md): install and get a working engine in minutes
- [Config File Reference](config-reference.md): every TOML field documented
- [Logging](logging.md): consistent log output across the entire OMOP stack
- [Inline & Session Usage](inline-usage.md): construct config in code without a file
- [Integration](integration.md): add `omop-config configure` support to your package
