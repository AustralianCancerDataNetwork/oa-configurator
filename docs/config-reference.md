# Config Reference

!!! note
    Configuration lives at `~/.config/omop/config.toml` by default. Override the path by setting
    `OA_CONFIG_PATH` to any `.toml` file (e.g. `OA_CONFIG_PATH=~/projects/omop.toml`).

---

## `[connections.<name>]`

One section per named physical connection: server address, credentials, target database. The name is referenced by databases (`[databases.*].connection`/`vocab_connection`).

| Field | Type | Required | Description |
|---|---|---|---|
| `dialect` | string | **yes** | SQLAlchemy dialect string, e.g. `postgresql+psycopg`, `mssql+pyodbc`, `sqlite` |
| `host` | string | no | Hostname or IP |
| `port` | int | no | Port number |
| `user` | string | no | Database username |
| `password` | string | no | Plaintext password *(see security note below)* |
| `database_name` | string | no | Database name on the server. For SQLite, use `:memory:` or an absolute path. |
| `read_only` | bool | `false` | Hint only; enforcement depends on the dialect |
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

A database maps logical OMOP CDM roles to named connections and schema names.

| Field | Type | Required | Description |
|---|---|---|---|
| `connection` | string | **yes** | Connection name (from `[connections.*]`) used as the primary CDM server |
| `vocab_connection` | string | no | Separate connection if vocabulary lives on a different server. Falls back to `connection`. |
| `cdm_schema` | string | no | Schema where CDM clinical tables live. Defaults to `"omop"`. |
| `vocab_schema` | string | no | Vocabulary schema. Falls back to `cdm_schema` when not set. |
| `results_schema` | string | no | Achilles / Atlas results schema |

### Example: all in one schema

```toml
[databases.cdm]
connection = "cdm"
cdm_schema = "omop"
```

### Example: separate vocab and results schemas

```toml
[databases.cdm]
connection     = "cdm"
cdm_schema     = "omop"
vocab_schema   = "omop_vocab"
results_schema = "results"
```

### Example: vocabulary on a separate server

```toml
[databases.cdm]
connection       = "cdm"
vocab_connection = "central_vocab"
cdm_schema       = "omop"
```

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

```toml
[models.nomic-embed]
provider        = "local-ollama"
model           = "nomic-embed-text:v1.5"
embedding_dim   = 768
document_prefix = "search_document: "
query_prefix    = "search_query: "

[models.nomic-embed.configuration]
temperature = 0.0
```

---

## `[tools.<name>]`

Per-package configuration. The `name` must match the package's `tool_name` class variable on its `PackageConfigBase` subclass. Fields are package-specific; each package defines its own typed schema, validated lazily against its `PackageConfigBase` subclass, not against a fixed set of fields oa-configurator itself knows about.

### Example: omop_emb

```toml
[tools.omop_emb]
backend             = "sqlitevec"
embedding_file_root = "/data/embeddings"
```

---

## `[logging]`

Controls log output for `oa_configurator` and any consuming packages. Defaults to WARNING with no handler until `configure_logging()` is called.

| Field | Type | Default | Description |
|---|---|---|---|
| `level` | string | `null` | Override the verbosity-derived level for all OMOP loggers: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `loggers` | table | `{}` | Fine-grained level overrides for specific loggers, e.g. `{"sqlalchemy.engine": "INFO"}` |

See [Logging](logging.md) for full details.
