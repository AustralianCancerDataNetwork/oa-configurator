# Inline & Session Usage

oa-configurator works without a TOML file. Two patterns cover the main use cases.

---

## `StackConfig.for_session()` — pure inline construction

Equivalent to loading a TOML file, but the config is built in code. Useful for:

- Notebooks and quick scripts where writing a file is overhead
- Tests that need an isolated, reproducible config
- Programmatic config generation (e.g. CI pipelines building config from secrets manager)

```python
from oa_configurator import StackConfig, ConnectionConfig, ResourceConfig, Resolver

config = StackConfig.for_session(
    connections={
        "local": ConnectionConfig(
            dialect="postgresql",
            host="localhost",
            database="omop",
            user="omop",
            password="omop",
        )
    },
    resources={
        "default": ResourceConfig(
            primary_db="local",
            omop_schema="cdm",
            vocab_schema="vocab",
        )
    },
)
engine = Resolver(config).resolve_resource("default").create_engine()
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `connections` | dict \| None | `{}` | Named `ConnectionConfig` objects |
| `resources` | dict \| None | `{}` | Named `ResourceConfig` objects |
| `tools` | dict \| None | `{}` | Named `ToolConfig` objects |
| `profiles` | dict \| None | `{}` | Named `ProfileConfig` objects |
| `settings` | `SettingsConfig` \| None | default `SettingsConfig` | Runtime settings |
| `base_path` | `Path` \| None | `Path.cwd()` | Anchor for resolving relative paths |

The `base_path` plays the same role as the directory containing the TOML file. Any `path`, `artifact_root`, or other filesystem field expressed as a relative string resolves relative to it.

### With `secret_source`

`secret_source` works exactly as in a file-based config — credentials are resolved from the environment or filesystem at resolution time.

```python
config = StackConfig.for_session(
    connections={
        "prod": ConnectionConfig(
            dialect="postgresql",
            host="prod.hospital.org",
            database="omop",
            user="omop_prod",
            secret_source="env:PROD_DB_PASSWORD",
        )
    },
    resources={"default": ResourceConfig(primary_db="prod")},
)
```

### Validation

Cross-references are validated at construction time, same as for file-loaded configs. A resource referencing an unknown connection raises immediately:

```python
StackConfig.for_session(
    connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
    resources={"default": ResourceConfig(primary_db="typo")},  # raises ValueError
)
```

---

## `Resolver.with_overrides()` — session-level override

Loads the shared config file, then replaces specific connections or resources for this session without touching the file. Useful for:

- Notebook sessions that need to redirect one resource to a local DuckDB
- Tests that swap prod connections for in-memory equivalents
- Sharing a team config but running with personal credentials locally

```python
from oa_configurator import load_stack_config, ConnectionConfig, ResourceConfig, Resolver

engine = (
    Resolver(load_stack_config())
    .with_overrides(
        connections={
            "local": ConnectionConfig(dialect="duckdb", path="local.duckdb")
        },
        resources={
            "default": ResourceConfig(primary_db="local")
        },
    )
    .resolve_resource("default")
    .create_engine()
)
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `connections` | dict \| None | Entries merged over the existing connections (new keys added; existing keys replaced) |
| `resources` | dict \| None | Entries merged over the existing resources |
| `tools` | dict \| None | Entries merged over the existing tools |

### What is preserved

- The original `settings` (including `active_profile`)
- All profiles and their overlays
- All connections, resources, and tools **not** mentioned in the overrides
- The path context (`configuration_base_path`, `secrets_dir`)

### What is validated

Cross-references are checked against the **merged** result. A resource override that references a connection that exists in neither the original config nor the override dict raises `ValueError` at call time.

```python
Resolver(load_stack_config()).with_overrides(
    resources={"default": ResourceConfig(primary_db="nonexistent")}   # raises
)
```

---

## Comparison

| | `for_session()` | `with_overrides()` |
|--|---|---|
| Needs a config file | No | Yes |
| Inherits shared team config | No | Yes |
| Profile overlays preserved | Only if explicitly passed | Yes |
| Path context | `base_path` parameter (default: cwd) | Inherited from loaded config |
| Primary use case | Scripts, tests, CI | Notebooks, per-user local redirects |
