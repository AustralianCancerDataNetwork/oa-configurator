# Integration

How oa-configurator connects to the rest of the OMOP Python stack.

---

## Dependency chain

```
oa-configurator   (no stack dependencies)
       ↓
  orm-loader      (no dependency on oa-configurator — stays neutral)
       ↓
 omop-alchemy     (depends on orm-loader; adopts oa-configurator for engine creation)
```

oa-configurator sits at the top. orm-loader stays neutral — it always receives a fully constructed SQLAlchemy engine from its caller.

---

## orm-loader

orm-loader has no dependency on oa-configurator. The connection pattern is:

```python
from oa_configurator import load_stack_config, Resolver
from orm_loader.helpers import bootstrap, Base

config = load_stack_config()
engine = Resolver(config).resolve_resource("default").create_engine()

bootstrap(engine, Base)          # create schema, set up tables
```

`bootstrap()` takes a SQLAlchemy engine. oa-configurator produces one. No changes to orm-loader are needed.

---

## omop-alchemy

omop-alchemy currently uses environment variables to locate the database engine:

```python
# Legacy pattern in omop_alchemy/config.py
engine_name = get_engine_name(schema="cdm")
engine      = create_engine_with_dependencies(engine_name)
```

The replacement pattern uses oa-configurator:

```python
from oa_configurator import load_stack_config, Resolver

config   = load_stack_config()
resource = Resolver(config).resolve_resource("default")
engine   = resource.create_engine()
```

`resource.create_engine()` automatically applies `schema_translate_map` from the resource's schema fields, replacing the multi-engine `ENGINE_CDM` / `ENGINE_VOCAB` approach with a single engine and SQLAlchemy's built-in schema translation.

### Schema mapping

omop-alchemy uses SQLAlchemy's `schema_translate_map` to route tables to different schemas at runtime. `ResolvedResource.create_engine()` wires this up automatically:

```python
resource = Resolver(config).resolve_resource("default")
# resource.omop_schema  = "cdm"
# resource.vocab_schema = "vocab"

engine = resource.create_engine()
# engine.get_execution_options()["schema_translate_map"]
# → {None: "cdm", "vocab": "vocab"}
```

Tables defined with `schema=None` route to `cdm`. Tables defined with `schema="vocab"` route to `vocab`.

You can also retrieve the map directly for passing to a session:

```python
from oa_configurator import schema_translate_map

stm     = schema_translate_map(resource)
session = Session(engine, execution_options={"schema_translate_map": stm})
```

### ENV → TOML migration guide

| Old env var | TOML equivalent |
|-------------|----------------|
| `ENGINE` | `connections.<name>` + `resources.<name>.primary_db = "<name>"` |
| `ENGINE_CDM` | `resources.<name>.primary_db = "<cdm_connection>"` + `omop_schema = "cdm"` |
| `ENGINE_VOCAB` | `resources.<name>.vocab_db = "<vocab_connection>"` + `vocab_schema = "vocab"` |
| `ENGINE_RESULTS` | `resources.<name>.results_db = "<results_connection>"` + `results_schema = "results"` |

---

## Logging

`configure_logging()` sets consistent log levels and output formats across the entire stack — `oa_configurator`, `orm_loader`, `omop_alchemy`, and future OMOP packages — in a single call.

### Typical usage

```python
from oa_configurator import load_stack_config, Resolver, configure_logging

config   = load_stack_config()
configure_logging(config)                        # applies [logging] block from TOML

resource = Resolver(config).resolve_resource("default")
engine   = resource.create_engine()
```

When `configure_logging(config)` is called with a `StackConfig`, it reads `config.logging`. If the TOML file has no `[logging]` block, the default `preset = "library"` applies — levels are set to WARNING and no handler is added, so the host application's root logger handles output.

### Standalone (no config file)

```python
from oa_configurator import configure_logging

configure_logging(preset="notebook")             # INFO → stdout, no timestamps
configure_logging(preset="application")          # INFO → stderr, with timestamps
configure_logging(preset="production")           # INFO → stdout, JSON lines
```

### Suppress noisy third-party loggers

Add a `[logging.loggers]` block to the config file:

```toml
[logging]
preset = "application"

[logging.loggers]
"sqlalchemy.engine" = "WARNING"
"sqlalchemy.pool"   = "WARNING"
```

This targets any fully-qualified Python logger name — not just stack namespaces.

See [Logging](logging.md) for the full reference including custom handler config and all preset details.

---

## Multi-database vs single-database deployments

**Single database, multiple schemas** (common):

```toml
[connections.local]
dialect  = "postgresql"
host     = "localhost"
database = "omop"
user     = "omop"
password = "omop"

[resources.default]
primary_db     = "local"
# vocab_db and results_db omitted → fall back to primary_db
omop_schema    = "cdm"
vocab_schema   = "vocab"
results_schema = "results"
```

`resource.vocab_db_is_primary_fallback` is `True` — the library signals that vocab and results are on the same physical server as primary. One engine, three schemas.

**Separate databases per role**:

```toml
[resources.default]
primary_db = "cdm_server"
vocab_db   = "vocab_server"
results_db = "results_server"
```

`resource.vocab_db_is_primary_fallback` is `False`. Callers that need separate engines create them via `resource.primary_db.create_engine()`, `resource.vocab_db.create_engine()`, etc.
