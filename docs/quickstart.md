# Quick Start

## Install

```bash
pip install oa-configurator
# or, if the stack uses uv:
uv add oa-configurator
```

## Option A — Config file (recommended for shared setups)

### 1. Create a config file with the CLI wizard

```bash
oa-config add-connection   # guided prompts for dialect, host, credentials
oa-config add-resource     # map the connection to a logical resource name
```

The wizard writes to `~/.config/omop/config.toml` by default.

### 2. Load and resolve in Python

```python
from oa_configurator import load_stack_config, Resolver

config   = load_stack_config()              # ~/.config/omop/config.toml
resolver = Resolver(config)

resource = resolver.resolve_resource("default")
engine   = resource.create_engine()         # SQLAlchemy Engine, ready to use
```

For interactive work (notebooks, REPLs), the namespace attributes give tab completion:

```python
resource = resolver.resources.default       # same as resolve_resource("default")
engine   = resource.primary_db.create_engine()
```

### 3. Configure logging (optional)

Call `configure_logging()` once at the top of your script or notebook. It sets consistent levels and formats across `orm_loader`, `omop_alchemy`, and every other OMOP stack package:

```python
from oa_configurator import configure_logging

configure_logging(preset="notebook")    # INFO to stdout, no timestamps
configure_logging(preset="application") # INFO to stderr, with timestamps
```

Or derive it from the loaded config:

```python
from oa_configurator import load_stack_config, configure_logging

config = load_stack_config()
configure_logging(config)               # applies config.logging
```

See [Logging](logging.md) for presets, TOML configuration, and per-logger overrides.

### 4. Use with orm-loader or omop-alchemy

```python
from orm_loader.helpers import bootstrap, Base

bootstrap(engine, Base)                     # create schema tables
```

```python
from omop_alchemy.oa_bridge import engine_from_config

engine = engine_from_config()               # reads config, resolves resource, returns Engine
```

---

## Option B — Inline construction (notebooks, quick scripts)

No config file required. Pass connections and resources directly:

```python
from oa_configurator import StackConfig, ConnectionConfig, ResourceConfig, Resolver

config = StackConfig.for_session(
    connections={
        "local": ConnectionConfig(
            dialect="postgresql",
            host="localhost",
            database="omop",
            secret_source="env:DB_PASSWORD",   # or: password="..."
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

See [Inline & Session Usage](inline-usage.md) for more patterns including session-level overrides.

---

## Environment overrides

Two environment variables are respected at load time:

| Variable | Description |
|----------|-------------|
| `OA_CONFIG_FILE` | Path to a different TOML file |
| `OA_ACTIVE_PROFILE` | Override `settings.active_profile` without editing the file |

```bash
OA_ACTIVE_PROFILE=prod python my_script.py
```

---

## Verify a config file

```bash
oa-config show                              # print validated config as JSON
oa-config resolve-resource default         # print the resolved resource bundle
```
