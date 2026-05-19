# Logging

oa-configurator provides a single call to configure consistent log output across the entire OMOP Python stack — `oa_configurator`, `orm_loader`, and `omop_alchemy` all share one logging namespace hierarchy, so one configuration controls them all.

---

## The problem

Without shared logging config each package does something different:

- orm-loader configures its own handler when `configure_logging()` is called
- omop-alchemy borrows orm-loader's helper but points at its own namespace
- Application code stacks `.env`-driven `load_dotenv()` calls on top

The result is duplicate log lines, mismatched levels between packages, and no obvious "turn on debug logging for everything" knob.

---

## Presets

`configure_logging()` accepts a `preset` that sets a sensible default level, handler, and format without requiring knowledge of Python's logging internals.

| Preset | Level | Handler | Format | Use case |
|--------|-------|---------|--------|---------|
| `library` | WARNING | none | — | **Default.** Library imported by another app. Never touches handlers. |
| `notebook` | INFO | stdout | simple (no timestamps) | Jupyter notebooks and interactive sessions |
| `application` | INFO | stderr | detailed (with timestamps) | CLI tools and scripts |
| `production` | INFO | stdout | JSON lines | Deployed services and log aggregators |

### Formats

| Name | Example output |
|------|---------------|
| `simple` | `INFO     orm_loader.loaders: Loading 45,000 rows into person` |
| `detailed` | `2024-03-15 14:22:01 [INFO    ] orm_loader.loaders: Loading 45,000 rows into person` |
| `json` | `{"time": "2024-03-15 14:22:01,123", "level": "INFO", "logger": "orm_loader.loaders", "message": "Loading 45,000 rows into person"}` |

All non-JSON formats pass output through a `RedactingFormatter` that replaces any `key=value` pair where the key matches a sensitive name (`password`, `secret`, `token`, `url`, etc.) with `key=<REDACTED>`.

---

## Usage patterns

### Quick one-liner (no config file needed)

```python
from oa_configurator import configure_logging

configure_logging(preset="notebook")     # INFO to stdout, clean format
configure_logging(preset="application")  # INFO to stderr, timestamps
configure_logging(preset="production")   # INFO to stdout, JSON lines
configure_logging(preset="library")      # WARNING, no handler (the default)
```

### From a loaded config file

```python
from oa_configurator import load_stack_config, configure_logging

config = load_stack_config()
configure_logging(config)                # applies config.logging
```

### Inline with overrides

```python
from oa_configurator import LoggingConfig, configure_logging

configure_logging(LoggingConfig(preset="notebook", level="DEBUG"))
```

---

## TOML configuration

Add a `[logging]` block to your config file. All fields are optional — omitting the block entirely is equivalent to `preset = "library"`.

```toml
[logging]
preset = "application"
```

### Override level

```toml
[logging]
preset = "application"
level  = "DEBUG"          # override the preset's INFO default
```

### Suppress noisy third-party loggers

```toml
[logging]
preset = "notebook"

[logging.loggers]
"sqlalchemy.engine" = "WARNING"   # suppress SQL statement echo
"sqlalchemy.pool"   = "WARNING"   # suppress connection pool lifecycle
"httpx"             = "WARNING"   # suppress if any HTTP clients are present
```

The `loggers` dict accepts any fully-qualified Python logger name, not just stack loggers.

### Custom handler

Override the preset's handler with `[logging.handler]`:

```toml
[logging]
preset = "application"

[logging.handler]
target = "file"
format = "detailed"
file_path = "logs/stack.log"      # relative to configuration_base_path
```

| `target` | Description |
|----------|-------------|
| `stderr` | Standard error stream (default for `application`) |
| `stdout` | Standard output stream (default for `notebook` and `production`) |
| `file` | File on disk; `file_path` is required |

### Full production example

```toml
[logging]
preset = "production"

[logging.loggers]
"sqlalchemy.engine" = "WARNING"
"sqlalchemy.pool"   = "WARNING"
```

### Full development example

```toml
[logging]
preset = "application"
level  = "DEBUG"

[logging.loggers]
"sqlalchemy.engine" = "WARNING"
```

---

## Controlled namespaces

`configure_logging()` sets levels on each of these logger namespaces:

| Namespace | Package |
|-----------|---------|
| `oa_configurator` | this package |
| `orm_loader` | orm-loader |
| `sql_loader` | orm-loader legacy (both configured during namespace transition) |
| `omop_alchemy` | omop-alchemy |
| `omop_emb` | omop-emb (future) |
| `omop_graph` | omop-graph (future) |

Because Python logging is hierarchical, setting `orm_loader` to INFO automatically applies to `orm_loader.tables.loadable_table`, `orm_loader.loaders`, and every other sub-logger.

The `loggers` dict can target any additional logger outside this set — including third-party libraries like `sqlalchemy`, `httpx`, or `urllib3`.

---

## Library vs application mode

When `preset = "library"` (the default):

- Levels are set on all stack namespaces
- **No handler is added** — the caller's own root logger handles output
- `propagate = True` on each namespace — records flow up to the root

This means importing oa-configurator never hijacks an application's logging setup.

When any other preset is used:

- A handler is added to each stack namespace logger
- `propagate = False` — records stop at the stack logger, preventing duplicates if the root also has handlers

---

## Downstream packages

orm-loader and omop-alchemy use standard `logging.getLogger(__name__)` calls.
No changes are needed in those packages — `configure_logging()` sets the level
on their namespace roots, and the records propagate (or don't) as configured.

```python
# This is all orm-loader or omop-alchemy needs to do — standard Python logging
import logging
logger = logging.getLogger(__name__)
logger.info("Loading vocabulary table")
```

The application entry point (or notebook cell) calls `configure_logging()` once,
and that setting applies to all log records from the entire stack.
