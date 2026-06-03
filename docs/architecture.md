# Architecture

## Purpose

`oa-configurator` is a shared configuration layer for the OMOP-oriented Python stack:

- `omop-alchemy`
- `orm-loader`
- `omop-emb`
- `omop-graph`
- `omop-spires`

It replaces per-package `.env` files, inconsistent env var lookups, and duplicated engine-creation boilerplate with a single typed TOML file and a common resolver interface.

---

## Core concepts

### Connection

A concrete database endpoint: dialect, host, credentials, database name. Stored in `[connections.<name>]`.

### Resource

A logical role bundle that maps OMOP CDM roles to connections and schema names:

- `primary_db`: the CDM server connection
- `vocab_db`: optional separate connection for vocabulary (falls back to `primary_db`)
- `cdm_schema`: schema where CDM clinical tables live (required)
- `vocab_schema` (optional): falls back to `cdm_schema`
- `results_schema` (optional): Achilles/Atlas results

### Tool

Per-package configuration in `[tools.<name>]`. The core model stores only `default_resource` and an `extra` dict. Each consuming package defines a typed `PackageConfigBase` subclass that provides a typed view over `extra`.

### Profile

A named overlay (`[profiles.<name>]`) that replaces specific connections, resources, or tools when active. Full model replacement, not a partial patch. Activate via `omop-config use <profile>` (persists to TOML) or `OA_ACTIVE_PROFILE=<profile>` (per-session).

---

## Data flow

```
~/.config/omop/config.toml
         │
         ▼
    load_stack_config()
         │  reads TOML → StackConfig
         │  applies active profile overlay
         ▼
       Resolver
         │
         ├─ resolve_connection(name)  → ResolvedDatabaseTarget
         │                               .url  (plaintext, internal)
         │                               .safe_url  (redacted, for logs)
         │                               .create_engine()
         │
         ├─ resolve_resource(name)   → ResolvedResource
         │                               .primary_db / .vocab_db  (ResolvedDatabaseTarget)
         │                               .cdm_schema / .vocab_schema / .results_schema
         │                               .schema_translate_map()
         │                               .create_engine(role="primary"|"vocab")
         │
         └─ resolve_tool(name)       → ResolvedToolConfig
                                         .extra  (raw dict; typed by PackageConfigBase.from_stack())
```

---

## Package integration via entry points

Consuming packages subclass `PackageConfigBase` and register via a `pyproject.toml` entry point:

```toml
[project.entry-points."omop.config"]
my_package = "my_package.config:MyPackageConfig"
```

`omop-config configure my_package` discovers the class at runtime via `importlib.metadata.entry_points(group="omop.config")`, presents the typed fields for interactive configuration, and writes the result to `[tools.my_package.extra]`. `oa-configurator` itself has no knowledge of any consuming package.

---

## Schema translate map

`ResolvedResource.schema_translate_map()` returns the SQLAlchemy-compatible schema translate dict:

```python
{None: "omop", "vocab": "omop_vocab", "results": "results"}
```

OMOP ORM models (omop-alchemy) carry `schema=None` or `schema="vocab"` on their `__table_args__`. The translate map routes them to the correct schema at runtime without changing model definitions.

---

## Security

Passwords are stored in plaintext in `~/.config/omop/config.toml`. Restrict permissions:

```bash
chmod 600 ~/.config/omop/config.toml
```

`ResolvedDatabaseTarget.safe_url` and `ResolvedDatabaseTarget.url` are distinct: `safe_url` has the password replaced with `***` and is used for all logging and display. The `.url` value (with plaintext password) is used only for engine creation and never logged by the library.

`RedactingFormatter` (applied by all non-library log presets) scrubs both `key=value` patterns and `://user:password@host` URL patterns from log output.

**Future work**: `secret_source` support (`env:VAR`, `file:path`, Vault, cloud secret managers) is planned but not implemented in this version.

---

## Config path

Always `~/.config/omop/config.toml`. The path is not configurable. Use `OA_ACTIVE_PROFILE` to select environments at runtime without editing the file.

---

## Future work

- `secret_source` on `ConnectionConfig`: `env:VARNAME`, `file:PATH`, Vault, cloud secret managers
- Async engine factory (`ResolvedDatabaseTarget.create_async_engine()`)
- Project-local overlay (`./oa-config.toml`) layered over user config
