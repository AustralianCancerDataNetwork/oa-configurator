# Inline & Session Usage

`oa-configurator` works without a TOML file. Two patterns cover the main use cases.

---

## `StackConfig.for_session()`: pure inline construction

Equivalent to loading a TOML file, but the config is built in code. Useful for:

- Notebooks and quick scripts where writing a file is overhead
- Tests that need an isolated, reproducible config
- Programmatic config generation (e.g. CI pipelines)

```python
from oa_configurator import StackConfig, ConnectionConfig, DatabaseConfig, Resolver

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
        "cdm": DatabaseConfig(
            connection="local",
            cdm_schema="omop",
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
| `databases` | dict \| None | `{}` | Named `DatabaseConfig` objects or raw dicts |
| `providers` | dict \| None | `{}` | Named `ProviderConfig` objects or raw dicts |
| `models` | dict \| None | `{}` | Named `ModelConfig` objects or raw dicts |
| `tools` | dict \| None | `{}` | Per-package `[tools.<name>]` sections, as plain dicts |

### Validation

Cross-references are validated at construction time, same as for file-loaded configs. A database referencing an unknown connection raises immediately:

```python
StackConfig.for_session(
    connections={"local": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
    databases={"cdm": DatabaseConfig(connection="typo", cdm_schema="omop")},  # raises ValueError
)
```

### In tests

`for_session()` is the recommended pattern for package tests. No file I/O, fully isolated:

```python
from oa_configurator import StackConfig, Resolver

def test_something():
    cfg = StackConfig.for_session(
        connections={"db": {"dialect": "sqlite", "database_name": ":memory:"}},
        databases={"cdm": {"connection": "db", "cdm_schema": "omop"}},
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
from oa_configurator import load_stack_config, ConnectionConfig, DatabaseConfig, Resolver

engine = (
    Resolver(load_stack_config())
    .with_overrides(
        connections={
            "local": ConnectionConfig(dialect="sqlite", database_name=":memory:")
        },
        databases={
            "cdm": DatabaseConfig(connection="local", cdm_schema="omop")
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
| `tools` | dict \| None | Entries merged over the existing tools |

### What is preserved

All connections, databases, providers, models, and tools **not** mentioned in the overrides.

### What is validated

Cross-references are checked against the **merged** result. A database override that references a connection that exists in neither the original config nor the override dict raises `ValueError` at call time.

```python
Resolver(load_stack_config()).with_overrides(
    databases={"cdm": DatabaseConfig(connection="nonexistent", cdm_schema="omop")}  # raises
)
```

---

## Comparison

| | `for_session()` | `with_overrides()` |
|--|---|---|
| Needs a config file | No | Yes |
| Inherits shared team config | No | Yes |
| Primary use case | Tests, scripts, CI | Notebooks, per-user local redirects |
