# Inline & Session Usage

`oa-configurator` can build, validate, and resolve configuration without reading a TOML file. This is useful for applications that collect settings in their own UI, notebooks, tests, and short-lived jobs.

## Plan a package change without writing

If your application offers its own configuration screen or API, use `plan_configure()` to turn submitted values into a complete candidate:

```python
from oa_configurator import plan_configure

candidate = plan_configure(
    MyPackageConfig,
    current_config,
    {"cdm_db": "cdm", "backend": "sqlite"},
)
```

Planning does not load or save a file and does not mutate `current_config`. If validation fails, keep showing the current settings and attach the returned errors to the proposed fields. Once the user approves the candidate, save it with `save_stack_config(candidate)`; keeping that call separate gives your application a natural preview or confirmation step.

---

## `StackConfig.for_session()`: pure inline construction

Equivalent to loading a TOML file, but the config is built in code. Useful for:

- Notebooks and quick scripts where writing a file is overhead
- Tests that need an isolated, reproducible config
- Programmatic config generation (e.g. CI pipelines)

```python
from oa_configurator import StackConfig, ConnectionConfig, CDMDatabaseConfig, Resolver

config = StackConfig.for_session(
    connections={
        "local": ConnectionConfig(
            dialect="postgresql+psycopg",
            host="localhost",
            database_name="omop",
            user="omop",
            password="omop",
        )
    },
    databases={
        "cdm": CDMDatabaseConfig(
            connection="local",
            schema_name="omop",
            vocab_schema="vocab",
        )
    },
)
engine = Resolver(config).resolve_database("cdm").create_engine()
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `connections` | dict \| None | `{}` | Named `ConnectionConfig` objects or raw dicts |
| `databases` | dict \| None | `{}` | Named `GenericDatabaseConfig`/`CDMDatabaseConfig` objects or raw dicts (each needs its own `kind`, see [Architecture](architecture.md#database)) |
| `providers` | dict \| None | `{}` | Named `ProviderConfig` objects or raw dicts |
| `models` | dict \| None | `{}` | Named `ModelConfig` objects or raw dicts |
| `vector_stores` | dict \| None | `{}` | Named `VectorStoreConfig` objects or raw dicts |
| `tools` | dict \| None | `{}` | Per-package `[tools.<name>]` sections, as plain dicts |

### Validation

Cross-references are validated at construction time, same as for file-loaded configs. A database referencing an unknown connection raises immediately:

```python
StackConfig.for_session(
    connections={"local": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
    databases={"cdm": CDMDatabaseConfig(connection="typo", schema_name="omop")},  # raises ValueError
)
```

### In tests

`for_session()` is the recommended pattern for package tests. No file I/O, fully isolated:

```python
from oa_configurator import StackConfig, Resolver

def test_something():
    cfg = StackConfig.for_session(
        connections={"db": {"dialect": "sqlite", "database_name": ":memory:"}},
        databases={"cdm": {"kind": "cdm", "connection": "db", "schema_name": "omop"}},
        tools={"my_package": {"backend": "test_backend"}},
    )
    resolver = Resolver(cfg)
    engine = resolver.resolve_database("cdm").create_engine()
    # ...
```

---

## `Resolver.with_overrides()`: session-level override

Loads the shared config file, then replaces specific connections or databases for this session without touching the file. Useful for:

- Tests that swap prod connections for in-memory equivalents
- Notebook sessions that redirect one database to a local connection
- Sharing a team config but running with personal credentials locally

```python
from oa_configurator import load_stack_config, ConnectionConfig, CDMDatabaseConfig, Resolver

engine = (
    Resolver(load_stack_config())
    .with_overrides(
        connections={
            "local": ConnectionConfig(dialect="sqlite", database_name=":memory:")
        },
        databases={
            "cdm": CDMDatabaseConfig(connection="local", schema_name="omop")
        },
    )
    .resolve_database("cdm")
    .create_engine()
)
```

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `connections` | dict \| None | Entries merged over the existing connections (new keys added; existing keys replaced) |
| `databases` | dict \| None | Entries merged over the existing databases |
| `providers` | dict \| None | Entries merged over the existing providers |
| `models` | dict \| None | Entries merged over the existing models |
| `vector_stores` | dict \| None | Entries merged over the existing vector stores |
| `tools` | dict \| None | Entries merged over the existing tools |

### What is preserved

All connections, databases, providers, models, vector stores, and tools **not** mentioned in the overrides.

### What is validated

Cross-references are checked against the **merged** result. A database override that references a connection that exists in neither the original config nor the override dict raises `ValueError` at call time.

```python
Resolver(load_stack_config()).with_overrides(
    databases={"cdm": CDMDatabaseConfig(connection="nonexistent", schema_name="omop")}  # raises
)
```

---

## Comparison

| | `for_session()` | `with_overrides()` |
|--|---|---|
| Needs a config file | No | Yes |
| Inherits shared team config | No | Yes |
| Primary use case | Tests, scripts, CI | Notebooks, per-user local redirects |
