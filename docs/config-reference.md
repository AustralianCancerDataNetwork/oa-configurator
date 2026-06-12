# Config Reference

!!! note
    Configuration lives at `~/.config/omop/config.toml` by default. Override the path by setting
    `OA_CONFIG_PATH` to any `.toml` file (e.g. `OA_CONFIG_PATH=~/projects/omop.toml`).
    Use `OA_ACTIVE_PROFILE` to switch profiles within a file at runtime.

---

## Top-level fields

| Field | Type | Default | Description |
|---|---|---|---|
| `active_profile` | string | `null` | Name of the profile to activate. Can be overridden by the `OA_ACTIVE_PROFILE` env var. |

---

## `[databases.<name>]`

One section per named database endpoint. The name is referenced by resources and profiles.

| Field | Type | Required | Description |
|---|---|---|---|
| `dialect` | string | **yes** | SQLAlchemy dialect string, e.g. `postgresql+psycopg`, `mssql+pyodbc`, `sqlite` |
| `host` | string | no | Hostname or IP |
| `port` | int | no | Port number |
| `user` | string | no | Database username |
| `password` | string | no | Plaintext password *(see security note below)* |
| `database_name` | string | no | Database name on the server. For SQLite, use `:memory:` or an absolute path. |
| `read_only` | bool | `false` | Hint only; enforcement depends on the dialect |

!!! warning "Security note"
    Passwords are stored in plaintext in this file. Restrict permissions with `chmod 600 ~/.config/omop/config.toml`. Secret-management support (env-backed passwords, Vault, etc.) is planned for a future release.

### Example: PostgreSQL

```toml
[databases.cdm]
dialect       = "postgresql+psycopg"
host          = "localhost"
port          = 5432
user          = "omop"
password      = "changeme"
database_name = "omop_cdm"
```

### Example: SQLite in-memory (for tests)

```toml
[databases.test_db]
dialect       = "sqlite"
database_name = ":memory:"
```

---

## `[resources.<name>]`

A resource maps logical OMOP CDM roles to named connections and schema names.

| Field | Type | Required | Description |
|---|---|---|---|
| `database` | string | **yes** | Database name (from `[databases.*]`) for the CDM server |
| `vocab_database` | string | no | Separate database if vocabulary lives on a different server. Falls back to `database`. |
| `cdm_schema` | string | **yes** | Schema where CDM clinical tables live |
| `vocab_schema` | string | no | Vocabulary schema. Falls back to `cdm_schema` when not set. |
| `results_schema` | string | no | Achilles / Atlas results schema |

### Example: all in one schema

```toml
[resources.default]
database   = "cdm"
cdm_schema = "omop"
```

### Example: separate vocab and results schemas

```toml
[resources.default]
database       = "cdm"
cdm_schema     = "omop"
vocab_schema   = "omop_vocab"
results_schema = "results"
```

### Example vocabulary on a separate server

```toml
[resources.default]
database      = "cdm"
vocab_database = "central_vocab"
cdm_schema    = "omop"
```

---

## `[tools.<name>]`

Per-package configuration. The `name` must match the package's `tool_name` class variable on its `PackageConfigBase` subclass.

| Field | Type | Default | Description |
|---|---|---|---|
| `default_resource` | string | `null` | Resource name this tool uses when none is specified |
| `extra` | table | `{}` | Package-specific key/value pairs. Each package defines its own typed fields that map here. |

### Example: omop_emb

```toml
[tools.omop_emb]
default_resource = "default"

[tools.omop_emb.extra]
backend             = "sqlitevec"
embedding_file_root = "/data/embeddings"
```

---

## `[profiles.<name>]`

A profile overlays connections, resources, and tools over the base config when active.

| Field | Type | Description |
|---|---|---|
| `databases` | table | Database configs that replace base databases with the same name, or add new ones |
| `resources` | table | Resource configs that replace base resources with the same name, or add new ones |
| `tools` | table | Tool configs that replace base tools with the same name, or add new ones |

Profile entries use the same field definitions as the base sections above.

**Example test profile pointing to a local test database**:

```toml
[profiles.test.databases.cdm]
dialect       = "postgresql+psycopg"
host          = "localhost"
port          = 5433
user          = "test_user"
password      = "test_pass"
database_name = "omop_test"

[profiles.test.resources.default]
database   = "cdm"
cdm_schema = "test_omop"
```

Activate it: `omop-config use test` or `OA_ACTIVE_PROFILE=test`.

---

## `[logging]`

Controls log output for `oa_configurator` and any consuming packages. Defaults to library preset (WARNING, no handler).

| Field | Type | Default | Description |
|---|---|---|---|
| `preset` | string | `"library"` | One of `library`, `notebook`, `application`, `production` |
| `level` | string | `null` | Override the preset's level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `loggers` | table | `{}` | Fine-grained level overrides for specific loggers, e.g. `{"sqlalchemy.engine": "INFO"}` |

See [Logging](logging.md) for full details.
