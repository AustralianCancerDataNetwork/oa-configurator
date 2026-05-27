# Logging

OA_Configurator provides a unified logging setup for the OMOP Python stack. All packages configure their loggers through the same call and formatter.

---

## Basic usage

```python
from oa_configurator import configure_logging

# Minimal — WARNING level (default)
configure_logging(extra_namespaces=["my_package"])

# From a loaded config file (uses [logging] block)
from oa_configurator import load_stack_config
configure_logging(load_stack_config(), extra_namespaces=["my_package"])
```

`configure_logging` is idempotent — safe to call multiple times with the same arguments.

---

## Verbosity

Log level is driven by the `verbosity` parameter, which maps directly to CLI `-v` count flags:

| `verbosity` | Level |
|-------------|-------|
| `0` (default) | WARNING |
| `1` (`-v`) | INFO |
| `2+` (`-vv`) | DEBUG |

```python
configure_logging(verbosity=1, extra_namespaces=["omop_graph"])
```

---

## Format

All log records use a single fixed format:

```
2026-01-15 14:32:01 | my_package | INFO | Connected to database
```

Output always goes to **stderr**. There are no alternative handlers or JSON formatters — route stderr to a log aggregator if needed.

---

## `extra_namespaces`

OA_Configurator never hardcodes downstream package names. Each consuming package passes its own namespace:

```python
configure_logging(verbosity=1, extra_namespaces=["omop_graph", "omop_emb"])
```

Both `oa_configurator` and all listed namespaces get the same level and handler.

---

## Level override from config file

The `[logging]` section in `config.toml` can set a fixed level that overrides `verbosity`:

```toml
[logging]
level = "DEBUG"

[logging.loggers]
"sqlalchemy.engine" = "INFO"
"httpx" = "WARNING"
```

`level` applies to all OMOP namespaces. `loggers` applies fine-grained overrides to any specific logger.

---

## `get_logger`

```python
from oa_configurator import get_logger

logger = get_logger("my_package.module")
logger.info("Hello from my package")
```

Thin wrapper around `logging.getLogger()` — a single import point for all packages.
