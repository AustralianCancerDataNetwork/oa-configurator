# Config Reference

!!! note
    Configuration lives at `~/.config/omop/config.toml` by default. Override the path by setting
    `OA_CONFIG_PATH` to any `.toml` file (e.g. `OA_CONFIG_PATH=~/projects/omop.toml`).
    The path is resolved when `oa_configurator` is first imported, so set the variable before
    starting the process. Changing it within a running process does not change `CONFIG_PATH`.

---

## `[connections.<name>]`

One section per named physical connection: server address, credentials, target database. The name is referenced by databases (`[databases.*].connection`/`vocab_connection`).

| Field | Type | Required | Description |
|---|---|---|---|
| `dialect` | string | **yes** | SQLAlchemy dialect string, e.g. `postgresql+psycopg`, `mssql+pyodbc`, `sqlite` |
| `host` | string | for non-SQLite | Hostname or IP. Required for every dialect except SQLite, which connects to a local file and has no host to speak of. |
| `port` | int | no | Port number |
| `user` | string | no | Database username |
| `password` | string | no | Plaintext password *(see security note below)* |
| `database_name` | string | for SQLite | Database name on the server. Required for SQLite (no implicit default): an absolute path, or `:memory:` for an in-memory database. |
| `test_only` | bool | `false` | Marks this connection as intended for testing only. Excluded from production database prompts; used as a safety check to prevent accidental test operations on production data. |

!!! warning "Security note"
    Passwords are stored in plaintext in this file. Restrict permissions with `chmod 600 ~/.config/omop/config.toml`. Secret-management support (env-backed passwords, Vault, etc.) is planned for a future release.

### Example: PostgreSQL

```toml
[connections.cdm]
dialect       = "postgresql+psycopg"
host          = "localhost"
port          = 5432
user          = "omop"
password      = "changeme"
database_name = "omop_cdm"
```

### Example: SQLite in-memory (for tests)

```toml
[connections.test_db]
dialect       = "sqlite"
database_name = ":memory:"
```

---

## `[databases.<name>]`

Every entry declares an explicit `kind`, discriminating which of the fields below apply. See [DatabaseKind](api/resources.md#databasekind) for the current members; there is no default and no inference from other fields.

| Field | Type | Required | Applies to | Description |
|---|---|---|---|---|
| `kind` | string | **yes** | both | Discriminator. See [DatabaseKind](api/resources.md#databasekind). |
| `connection` | string | **yes** | both | Connection name (from `[connections.*]`) used as the primary server |
| `schema_name` | string | no | both | Schema this database's tables live in. Defaults to `"omop"` for the CDM kind only; the generic kind has no default (unset means "use the connection's own default"). |
| `vocab_connection` | string | no | CDM only | Separate connection if vocabulary lives on a different server. Falls back to `connection`. |
| `vocab_schema` | string | no | CDM only | Vocabulary schema. Falls back to `schema_name` when not set. |
| `results_schema` | string | no | CDM only | Achilles / Atlas results schema |

A `RefTo` naming one kind rejects an entry of the other, at construction time, with a "wrong kind" error distinct from "doesn't exist" (`mismatched_kind_refs`).

### Example: a generic database (e.g. a vector store's own tables)

```toml
[databases.emb_db]
kind       = "generic"
connection = "emb"
```

### Example: CDM, all in one schema

```toml
[databases.cdm_db]
kind        = "cdm"
connection  = "cdm"
schema_name = "omop"
```

### Example: CDM, separate vocab and results schemas

```toml
[databases.cdm_db]
kind           = "cdm"
connection     = "cdm"
schema_name    = "omop"
vocab_schema   = "omop_vocab"
results_schema = "results"
```

### Example: CDM, vocabulary on a separate server

```toml
[databases.cdm_db]
kind              = "cdm"
connection        = "cdm"
vocab_connection  = "central_vocab"
schema_name       = "omop"
```

---

## `[vector_stores.<name>]`

Which storage backend an embedding-capable package should use. Referenced by a consuming package's own field (e.g. `vector_store_name`), the same way `[databases.*]`/`[models.*]` are.

| Field | Type | Required | Description |
|---|---|---|---|
| `backend_type` | string | **yes** | Storage backend key, e.g. `sqlitevec`, `pgvector`. A plain string validated by the owning package (e.g. `omop-emb`), not by `oa-configurator` itself. |
| `database` | string | **yes** | Name of a `[databases.*]` entry (from `[databases.*]`), which must have `kind = "generic"` |
| `faiss_cache_dir` | string | no | Directory to cache FAISS index files, if the consuming package uses one |
| `configuration` | table | `{}` | Free-form per-store knobs with no dedicated field, passed through verbatim |

```toml
[vector_stores.vector_store]
backend_type = "pgvector"
database     = "emb_db"
```

A sqlite-backed store is expressed the same way as pgvector: `database` points at a `[databases.*]` entry whose own `[connections.*]` entry has `dialect = "sqlite"`. There is no separate `sqlite_path` field.

---

## `[providers.<name>]`

A concrete LLM/embedding provider connection. Referenced by `[models.*].provider`. Managed via `omop-config providers add <name>` / `omop-config providers list`, or hand-edited.

| Field | Type | Required | Description |
|---|---|---|---|
| `provider` | string | **yes** | Provider key, e.g. `ollama`, `llamacpp`, `vllm`, `openai`, `anthropic`, `gemini` |
| `base_url` | string | no | Base URL for this deployment (a local server, a cloud vendor endpoint, and so on) |
| `api_key` | string | no | Plaintext API key *(see security note above)* |

```toml
[providers.local-ollama]
provider = "ollama"
base_url = "http://localhost:11434"
```

---

## `[models.<name>]`

A named, reusable, concretely-configured model, served through a `[providers.*]` entry. Consuming packages reference it by name (e.g. an `embedding_model_name` field on their own `[tools.*]` extra just names an entry here). Managed via `omop-config models add <name>` / `omop-config models list`, or hand-edited.

| Field | Type | Required | Description |
|---|---|---|---|
| `provider` | string | **yes** | Name of the provider entry (from `[providers.*]`) this model is served through |
| `model` | string | **yes** | Model name or identifier passed to the provider |
| `embedding_dim` | int | no | Embedding dimension override. Unset lets the provider's own discovery determine it. |
| `document_prefix` | string | no | Prefix prepended to document/passage text before embedding, for asymmetric embedding models (e.g. nomic-embed-text, E5, BGE) |
| `query_prefix` | string | no | Prefix prepended to query text before embedding, for asymmetric embedding models |
| `configuration` | table | `{}` | Free-form per-model knobs (`max_tokens`, `temperature`, and so on) with no dedicated field, passed through verbatim |
| `embeddings` | bool | false | Whether this specific model supports the embeddings endpoint. Required for any model used for embedding. |
| `tool_use` | bool | false | Whether this specific model supports tool/function calling |
| `structured_output` | bool | false | Whether this specific model supports structured (schema-constrained) output |
| `extended_thinking` | bool | false | Whether this specific model supports reasoning/extended-thinking output |

```toml
[models.nomic-embed]
provider        = "local-ollama"
model           = "nomic-embed-text:v1.5"
embedding_dim   = 768
document_prefix = "search_document: "
query_prefix    = "search_query: "
embeddings      = true

[models.nomic-embed.configuration]
temperature = 0.0
```

---

## `[tools.<name>]`

Per-package configuration. The `name` must match the package's `tool_name` class variable on its `PackageConfigBase` subclass. Fields are package-specific; each package defines its own typed schema, validated lazily against its `PackageConfigBase` subclass, not against a fixed set of fields oa-configurator itself knows about.

### Example: omop_emb

```toml
[tools.omop_emb]
cdm_db               = "cdm_db"
embedding_model_name = "embedding-model"
vector_store_name    = "vector_store"
```

---

## `[logging]`

Controls log output for `oa_configurator` and any consuming packages. Defaults to WARNING with no handler until `configure_logging()` is called.

| Field | Type | Default | Description |
|---|---|---|---|
| `level` | string | `null` | Override the verbosity-derived level for all OMOP loggers: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `loggers` | table | `{}` | Fine-grained level overrides for specific loggers, e.g. `{"sqlalchemy.engine": "INFO"}` |

See [Logging](logging.md) for full details.
