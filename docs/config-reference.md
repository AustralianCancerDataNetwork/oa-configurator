# Config File Reference

Configuration is stored in TOML. The default path is `~/.config/omop/config.toml`; override with `OA_CONFIG_FILE`.

A minimal working file:

```toml
[connections.local]
dialect    = "postgresql"
host       = "localhost"
database   = "omop"
user       = "omop"
password   = "omop"

[resources.default]
primary_db = "local"
```

---

## `[settings]`

Optional top-level block. All fields have defaults.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `active_profile` | string | `"default"` | Name of the profile to apply. Overridable via `OA_ACTIVE_PROFILE`. |
| `configuration_base_path` | string | `"."` | Base directory for resolving relative paths. `"."` means the directory containing this file. Any other value must be an absolute path. |
| `secrets_dir` | string \| null | `null` | Optional directory for file-backed secrets. Relative to `configuration_base_path`. |

```toml
[settings]
active_profile         = "prod"
configuration_base_path = "."
secrets_dir            = "secrets"
```

---

## `[connections.<name>]`

One block per named connection. Referenced by resources via their name.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `dialect` | string | yes | SQLAlchemy drivername: `postgresql`, `postgresql+psycopg`, `sqlite`, `duckdb`, etc. |
| `host` | string | for network DBs | Hostname or IP |
| `port` | int | no | Port number; driver default used if omitted |
| `user` | string | for network DBs | Database user |
| `database` | string | for network DBs | Database name |
| `password` | string | mutually exclusive with `secret_source` | Inline password (avoid for shared files) |
| `secret_source` | string | mutually exclusive with `password` | Indirect credential — see [Secrets](secrets.md) |
| `path` | string | for file-backed DBs | Local file path (SQLite, DuckDB). Relative to `configuration_base_path`. |
| `kind` | `"database"` \| `"file"` | no | Default: `"database"`. Use `"file"` for local file-backed connections. |
| `read_only` | bool | no | Default: `false`. Marks the connection as read-only. |
| `engine_kwargs` | dict | no | Extra keyword arguments passed through to `sqlalchemy.create_engine()` (e.g. `pool_size`, `echo`). |

### Network database

```toml
[connections.prod_cdm]
dialect       = "postgresql"
host          = "prod.hospital.org"
port          = 5432
user          = "omop_prod"
secret_source = "file:prod_cdm.password"
database      = "omop_cdm"
```

### SQLite file

```toml
[connections.local_sqlite]
dialect  = "sqlite"
path     = "data/omop.sqlite"
```

### DuckDB file

```toml
[connections.local_duckdb]
dialect  = "duckdb"
kind     = "file"
path     = "data/omop.duckdb"
read_only = true
```

### Engine kwargs

```toml
[connections.prod_cdm]
dialect       = "postgresql"
host          = "prod.hospital.org"
database      = "omop_cdm"
user          = "omop_prod"
secret_source = "env:PROD_CDM_PASSWORD"
engine_kwargs = { pool_pre_ping = true, pool_size = 5 }
```

---

## `[resources.<name>]`

Maps logical roles to named connections and schema / path settings.
`primary_db` is the only required field; all others default to `null`.

| Field | Type | Description |
|-------|------|-------------|
| `primary_db` | string | **Required.** Connection name for the main OMOP/CDM database. |
| `vocab_db` | string \| null | Connection name for the vocabulary database. Falls back to `primary_db` when omitted. |
| `results_db` | string \| null | Connection name for the results / cohort database. Falls back to `primary_db` when omitted. |
| `omop_schema` | string \| null | Schema name for OMOP clinical tables. Maps to SQLAlchemy `schema_translate_map = {None: ...}`. |
| `vocab_schema` | string \| null | Schema name for vocabulary tables. Maps to `schema_translate_map = {"vocab": ...}`. |
| `results_schema` | string \| null | Schema name for results tables. Maps to `schema_translate_map = {"results": ...}`. |
| `athena_source_path` | string \| null | Local path to an Athena OHDSI vocabulary download. |
| `artifact_root` | string \| null | Root directory for all derived artifacts. |
| `embedding_file_root` | string \| null | Directory for vector embedding files (FAISS, etc.). |
| `analytic_db_file_root` | string \| null | Directory for local analytic DB files (DuckDB exports, etc.). |

All path fields resolve relative to `configuration_base_path`.

```toml
[resources.default]
primary_db             = "local_cdm"
vocab_db               = "local_cdm"
results_db             = "local_cdm"
omop_schema            = "cdm"
vocab_schema           = "cdm"
results_schema         = "results"
artifact_root          = "artifacts"
embedding_file_root    = "artifacts/embeddings"
analytic_db_file_root  = "artifacts/databases"
```

---

## `[tools.<name>]`

Per-tool defaults. Tool names are arbitrary — they match whatever name the consuming tool looks for.

| Field | Type | Description |
|-------|------|-------------|
| `default_resource` | string \| null | Name of the resource this tool uses by default. Must reference a key in `resources`. |
| `backend` | string \| null | Backend identifier for tools that support multiple backends (e.g. `"pgvector"`, `"faiss"`). |
| `embedding_file_root` | string \| null | Tool-specific override for the embedding directory. |
| `database_file_root` | string \| null | Tool-specific override for the analytic DB directory. |
| `extra` | dict[string, string] | Arbitrary key/value pairs for tool-specific options not covered above. |

```toml
[tools.omop_emb]
default_resource    = "default"
backend             = "pgvector"
embedding_file_root = "artifacts/embeddings"

[tools.orm_loader]
default_resource    = "default"
database_file_root  = "artifacts/databases"
```

---

## `[profiles.<name>]`

Named environments that patch resources and tools without duplicating full blocks.
See [Profiles & Overlays](profiles.md) for the full guide.

| Field | Type | Description |
|-------|------|-------------|
| `description` | string \| null | Human-readable description. |
| `resource_overrides.<resource_name>` | partial ResourceConfig | Overrides applied to the named resource when this profile is active. |
| `tool_overrides.<tool_name>` | partial ToolConfig | Overrides applied to the named tool when this profile is active. |

```toml
[profiles.prod]
description = "remote OMOP source, local derived artifacts"

[profiles.prod.resource_overrides.default]
primary_db  = "prod_cdm"
vocab_db    = "prod_vocab"

[profiles.prod.tool_overrides.omop_emb]
embedding_file_root = "artifacts/prod/embeddings"
```

---

## Complete annotated example

See [`examples/config.toml`](https://github.com/AustralianCancerDataNetwork/oa-configurator/blob/main/examples/config.toml) in the repository for a full two-profile, multi-connection example.
