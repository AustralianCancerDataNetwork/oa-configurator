# oa-configurator

A shared configuration layer for the OMOP-oriented Python stack.

---

**oa-configurator** gives tools like `omop-alchemy`, `orm-loader`, `omop-emb`, and `omop-graph` a single typed configuration file instead of a tangle of environment variables and package-local `.env` files.

## Key Concepts

| Concept | Description |
|---------|-------------|
| **connection** | A concrete database endpoint (host, dialect, credentials) |
| **resource** | A logical role bundle — primary OMOP DB, vocab DB, results DB, artifact paths |
| **profile** | A named environment (e.g. `local`, `prod`) that patches resources and tools |
| **tool** | Per-tool defaults — backend, default resource, storage roots |
| **logging** | One call configures consistent log output for the entire OMOP Python stack |

Configuration lives in one TOML file (default `~/.config/omop/config.toml`) and is loaded once. The **Resolver** turns logical names into typed, credential-resolved handles ready for use.

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
    from oa_configurator import StackConfig, ConnectionConfig, ResourceConfig, Resolver

    config = StackConfig.for_session(
        connections={"local": ConnectionConfig(dialect="postgresql", host="localhost",
                                               database="omop", password="omop")},
        resources={"default": ResourceConfig(primary_db="local", omop_schema="cdm")},
    )
    engine = Resolver(config).resolve_resource("default").create_engine()
    ```

=== "Session override"

    ```python
    from oa_configurator import load_stack_config, ConnectionConfig, ResourceConfig, Resolver

    # Load shared team config, redirect one resource to a local DuckDB
    engine = (
        Resolver(load_stack_config())
        .with_overrides(
            connections={"local": ConnectionConfig(dialect="duckdb", path="local.duckdb")},
            resources={"default": ResourceConfig(primary_db="local")},
        )
        .resolve_resource("default")
        .create_engine()
    )
    ```

## Next Steps

- [Quick Start](quickstart.md) — install and get a working engine in minutes
- [Config File Reference](config-reference.md) — every TOML field documented
- [Logging](logging.md) — consistent log output across the entire OMOP stack
- [Inline & Session Usage](inline-usage.md) — construct config in code without a file
- [Profiles & Overlays](profiles.md) — switch between environments cleanly
- [Secrets](secrets.md) — keep passwords out of the config file
