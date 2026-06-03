# Inline & Session Usage

`oa-configurator` works without a TOML file. Two patterns cover the main use cases.

---

## `StackConfig.for_session()`: pure inline construction

Equivalent to loading a TOML file, but the config is built in code. Useful for:

- Notebooks and quick scripts where writing a file is overhead
- Tests that need an isolated, reproducible config
- Programmatic config generation (e.g. CI pipelines)

```python
from oa_configurator import StackConfig, ConnectionConfig, ResourceConfig, Resolver

config = StackConfig.for_session(
    connections={
        "local": ConnectionConfig(
            dialect="postgresql+psycopg",
            host="localhost",
            database="omop",
            user="omop",
            password="omop",
        )
    },
    resources={
        "default": ResourceConfig(
            primary_db="local",
            cdm_schema="cdm",
            vocab_schema="vocab",
        )
    },
)
engine = Resolver(config).resolve_resource("default").create_engine()
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `connections` | dict \| None | `{}` | Named `ConnectionConfig` objects or raw dicts |
| `resources` | dict \| None | `{}` | Named `ResourceConfig` objects or raw dicts |
| `tools` | dict \| None | `{}` | Named `ToolConfig` objects or raw dicts |
| `profiles` | dict \| None | `{}` | Named `ProfileOverrideConfig` objects or raw dicts |
| `active_profile` | str \| None | `None` | Profile to activate |

### Validation

Cross-references are validated at construction time, same as for file-loaded configs. A resource referencing an unknown connection raises immediately:

```python
StackConfig.for_session(
    connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
    resources={"default": ResourceConfig(primary_db="typo", cdm_schema="omop")},  # raises ValueError
)
```

### In tests

`for_session()` is the recommended pattern for package tests. No file I/O, fully isolated:

```python
from oa_configurator import StackConfig, Resolver

def test_something():
    cfg = StackConfig.for_session(
        connections={"db": {"dialect": "sqlite", "database": ":memory:"}},
        resources={"default": {"primary_db": "db", "cdm_schema": "omop"}},
        tools={"my_package": {"extra": {"backend": "test_backend"}}},
    )
    resolver = Resolver(cfg)
    engine = resolver.resolve_resource("default").create_engine()
    # ...
```

---

## `Resolver.with_overrides()`: session-level override

Loads the shared config file, then replaces specific connections or resources for this session without touching the file. Useful for:

- Tests that swap prod connections for in-memory equivalents
- Notebook sessions that redirect one resource to a local database
- Sharing a team config but running with personal credentials locally

```python
from oa_configurator import load_stack_config, ConnectionConfig, ResourceConfig, Resolver

engine = (
    Resolver(load_stack_config())
    .with_overrides(
        connections={
            "local": ConnectionConfig(dialect="sqlite", database=":memory:")
        },
        resources={
            "default": ResourceConfig(primary_db="local", cdm_schema="omop")
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

- The original active profile
- All profiles and their overlays
- All connections, resources, and tools **not** mentioned in the overrides

### What is validated

Cross-references are checked against the **merged** result. A resource override that references a connection that exists in neither the original config nor the override dict raises `ValueError` at call time.

```python
Resolver(load_stack_config()).with_overrides(
    resources={"default": ResourceConfig(primary_db="nonexistent", cdm_schema="omop")}  # raises
)
```

---

## Comparison

| | `for_session()` | `with_overrides()` |
|--|---|---|
| Needs a config file | No | Yes |
| Inherits shared team config | No | Yes |
| Profile overlays preserved | Only if explicitly passed | Yes |
| Primary use case | Tests, scripts, CI | Notebooks, per-user local redirects |
