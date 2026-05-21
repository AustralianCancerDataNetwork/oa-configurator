# Logging

OA_Configurator provides a unified logging setup for the OMOP Python stack. All packages configure their loggers through the same call, using the same presets and redaction rules.

---

## Basic usage

```python
from oa_configurator import configure_logging

# Minimal — from a preset
configure_logging(preset="application", extra_namespaces=["my_package"])

# From config file
from oa_configurator import load_stack_config
configure_logging(load_stack_config(), extra_namespaces=["my_package"])
```

`configure_logging` is idempotent — safe to call multiple times with the same arguments.

---

## Presets

| Preset | Level | Handler | Format |
|---|---|---|---|
| `library` | WARNING | none (propagates to root) | — |
| `notebook` | INFO | stdout | simple `LEVEL name: message` |
| `application` | INFO | stderr | detailed with timestamps |
| `production` | INFO | stdout | newline-delimited JSON |

`library` is the default and is safe for use inside library code: it never installs handlers or touches the caller's logging configuration.

---

## `extra_namespaces`

OA_Configurator never hardcodes downstream package names. Each consuming package passes its own namespace:

```python
configure_logging(preset="application", extra_namespaces=["omop_graph", "omop_emb"])
```

Both `oa_configurator` and all listed namespaces get the same level and handler.

---

## Level and logger overrides

Override the preset level:

```python
from oa_configurator.logging_config import LoggingConfig

configure_logging(LoggingConfig(preset="application", level="DEBUG"))
```

Fine-grained overrides for specific loggers:

```toml
[logging]
preset = "application"

[logging.loggers]
"sqlalchemy.engine" = "INFO"
"httpx" = "WARNING"
```

---

## `get_logger`

```python
from oa_configurator import get_logger

logger = get_logger("my_package.module")
logger.info("Hello from my package")
```

Thin wrapper around `logging.getLogger()` — a single import point for all packages.

---

## Password redaction

`RedactingFormatter` (applied by all non-library presets) scrubs two patterns:

1. `key=value` patterns: `password=secret` → `password=<REDACTED>`
2. URL passwords: `postgresql://user:secret@host/db` → `postgresql://user:***@host/db`

Import it directly if you need a custom handler:

```python
from oa_configurator import RedactingFormatter
import logging

handler = logging.StreamHandler()
handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
```
