# oa-configurator

A shared configuration layer for the OMOP-oriented Python stack.

---

**oa-configurator** gives OMOP tools a single typed configuration file instead of a tangle of environment variables and package-local `.env` files.

## Key Concepts
- **Connection**: A concrete database endpoint (host, dialect, credentials)
- **Resource**:  A logical role bundle, e.g. primary OMOP CDM DB, embedding DB, artficat paths
- **Profile**: A named environment (e.g. `local`, `prod`) that patches resources and tools
- **Tool**:  Per-tool defaults, e.g. backend, default resource, storage roots
- **Logging**: One call configures consistent log output for the entire OMOP Python stack

!!! info
    Configuration lives in one TOML file (default **`~/.config/omop/config.toml`**) and is loaded once. The Resolver turns logical names into typed, credential-resolved handles ready for use.

## Quick Example

=== "From a config file"

    ```python
    from oa_configurator import load_stack_config, Resolver

    config = load_stack_config()                        # reads ~/.config/omop/config.toml
    resolver = Resolver(config)

    resource = resolver.resolve_resource("default")
    engine   = resource.create_engine()                # SQLAlchemy Engine, schema_translate_map applied
    ```

=== "Inline (no file)"

    ```python
    from oa_configurator import StackConfig, DatabaseConfig, ResourceConfig, Resolver

    config = StackConfig.for_session(
        databases={"local": DatabaseConfig(dialect="postgresql", host="localhost",
                                           database_name="omop", password="omop")},
        resources={"default": ResourceConfig(database="local", cdm_schema="cdm")},
    )
    engine = Resolver(config).resolve_resource("default").create_engine()
    ```

=== "Session override"

    ```python
    from oa_configurator import load_stack_config, DatabaseConfig, ResourceConfig, Resolver

    # Load shared team config, redirect one resource to a local SQLite database
    engine = (
        Resolver(load_stack_config())
        .with_overrides(
            databases={"local": DatabaseConfig(dialect="sqlite", database_name="/data/local.db")},
            resources={"default": ResourceConfig(database="local", cdm_schema="omop")},
        )
        .resolve_resource("default")
        .create_engine()
    )
    ```

## Next Steps

- [Quick Start](quickstart.md): install and get a working engine in minutes
- [Config File Reference](config-reference.md): every TOML field documented
- [Logging](logging.md): consistent log output across the entire OMOP stack
- [Inline & Session Usage](inline-usage.md): construct config in code without a file
- [Profiles & Overlays](profiles.md): switch between environments cleanly
- [Integration](integration.md): add `omop-config configure` support to your package
