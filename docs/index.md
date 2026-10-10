# oa-configurator

A shared configuration layer for the OMOP-oriented Python stack.

---

**oa-configurator** gives OMOP tools a single typed configuration file instead of a tangle of environment variables and package-local `.env` files.

## Key Concepts
- **Connection**: A concrete database endpoint (host, dialect, credentials)
- **Database**: A named database built on a connection. Comes in two kinds (see [Architecture](architecture.md#database)): a plain generic database, or a CDM database with its vocab/results role bundle
- **Provider** / **Model**: The same two-tier pattern as Connection/Database, for LLM and embedding backends
- **Vector Store**: Which storage backend an embedding-capable package should use, pointing at a generic database
- **Tool**: Per-tool settings, e.g. which database/model/vector store a package uses
- **Logging**: One call configures consistent log output for the entire OMOP Python stack

!!! info
    Configuration lives in one TOML file (default **`~/.config/omop/config.toml`**, overridable via `OA_CONFIG_PATH`) and is loaded once. The Resolver turns logical names into typed, credential-resolved handles ready for use.

## Quick Example

=== "From a config file"

    ```python
    from oa_configurator import load_stack_config, Resolver

    config = load_stack_config()                        # reads the active config path (default ~/.config/omop/config.toml)
    resolver = Resolver(config)

    database = resolver.resolve_database("cdm")
    primary, vocab = database.create_engines()          # SQLAlchemy Engines, schema_translate_map applied
    ```

=== "Inline (no file)"

    ```python
    from oa_configurator import StackConfig, ConnectionConfig, CDMDatabaseConfig, Resolver

    config = StackConfig.for_session(
        connections={"local": ConnectionConfig(dialect="postgresql+psycopg", host="localhost",
                                                database_name="omop", password="omop")},
        databases={"cdm": CDMDatabaseConfig(connection="local", cdm_schema="omop")},
    )
    primary, vocab = Resolver(config).resolve_database("cdm").create_engines()
    ```

=== "Session override"

    ```python
    from oa_configurator import load_stack_config, ConnectionConfig, CDMDatabaseConfig, Resolver

    # Load shared team config, redirect one database to a local SQLite connection
    primary, vocab = (
        Resolver(load_stack_config())
        .with_overrides(
            connections={"local": ConnectionConfig(dialect="sqlite", database_name="/data/local.db")},
            databases={"cdm": CDMDatabaseConfig(connection="local")},
        )
        .resolve_database("cdm")
        .create_engines()
    )
    ```

## Next Steps

- [Migrating from 0.x](migration-1.0.md): upgrading an existing config? Start here.
- [Upgrading from 1.x](upgrading-from-1.x.md): update a 1.x CDM config for 2.0.
- [Quick Start](quickstart.md): install and get a working engine in minutes
- [Config File Reference](config-reference.md): every TOML field documented
- [Logging](logging.md): consistent log output across the entire OMOP stack
- [Inline & Session Usage](inline-usage.md): construct config in code without a file
- [Integration](integration.md): add `omop-config configure` support to your package
