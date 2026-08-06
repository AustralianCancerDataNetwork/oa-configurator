# Architecture

## Purpose

`oa-configurator` is a shared configuration layer for the OMOP-oriented Python stack:

- `omop-alchemy`
- `orm-loader`
- `omop-emb`
- `omop-graph`
- `omop-spires`
- `groundworkers`

It replaces per-package `.env` files, inconsistent env var lookups, and duplicated engine-creation boilerplate with a single typed TOML file and a common resolver interface.

---

## Core concepts

### Connection

A concrete database endpoint: dialect, host, credentials, database name. Stored in `[connections.<name>]`.

```toml
[connections.cdm]
dialect       = "postgresql+psycopg"
host          = "localhost"
port          = 5432
user          = "omop"
password      = "changeme"
database_name = "omop_cdm"
```

### Database

Every `[databases.<name>]` entry declares an explicit `kind` (see [DatabaseKind](api/resources.md#databasekind) for the current members), no default, no inference. The kind decides which concrete fields exist on top of the shared `connection`/`schema_name` base; see [Resources](api/resources.md#genericdatabaseconfig) for the field list each kind adds (only the CDM kind carries the vocab/results role bundle, and only it defaults `schema_name` to `"omop"`).

```toml
[databases.emb_db]
kind       = "generic"
connection = "emb"

[databases.cdm_db]
kind        = "cdm"
connection  = "cdm"
schema_name = "omop"
```

This isn't a duplication of `Role` below: `kind` decides which fields an entry has at config-authoring time, `Role` selects among a CDM entry's several connections at resolve time. A generic entry only ever has one connection, so there's nothing for `Role` to select there.

A `RefTo` targeting one kind rejects an entry of the other at construction time (`mismatched_kind_refs`), the same way a `RefTo` targeting the wrong *section* already did.

### Vector store

Which storage backend an embedding-capable package should use: `backend_type` (a plain string like `"sqlitevec"`/`"pgvector"`, validated by the owning package, not here), a `database` naming a *generic* `[databases.*]` entry, an optional `faiss_cache_dir`, and a free-form `configuration` table for anything else with no dedicated field. Stored in `[vector_stores.<name>]`.

```toml
[vector_stores.vector_store]
backend_type = "pgvector"
database     = "emb_db"
```

A third instance of the connection/database, provider/model pattern, this time for "which storage backend does an embedding subsystem use." Unlike provider/model, there's only one tier here: a vector store points straight at a `[databases.*]` entry rather than introducing its own leaf-tier section.

### Provider

A concrete LLM/embedding provider connection: provider key, base URL, API key. Stored in `[providers.<name>]`. Peer of `Connection` for LLM backends instead of databases.

```toml
[providers.local-ollama]
provider = "ollama"
base_url = "http://localhost:11434"
```

### Model

A named, reusable, concretely-configured model: which provider it runs through, model name, embedding dimension, prefixes. Stored in `[models.<name>]`, references a `Provider` by name. Peer of `Database` for LLM backends instead of databases. See [Database/Model resolution](#databasemodel-resolution) below: the two pairs resolve the same way.

```toml
[models.nomic-embed]
provider = "local-ollama"
model    = "nomic-embed-text:v1.5"
```

### Tool

Per-package configuration in `[tools.<name>]`. The core model stores it as a plain, untyped dict; each consuming package defines a typed `PackageConfigBase` subclass that provides a validated view over it, resolved lazily since packages register via entry points and aren't known to `oa-configurator` itself at parse time.

```toml
[tools.omop_emb]
cdm_db                = "cdm_db"
embedding_model_name  = "embedding-model"
vector_store_name     = "vector_store"
```

### RefTo

The one generic marker behind every cross-reference in the config, whether between two core sections (`DatabaseConfig.connection` naming a `[connections.*]` entry) or from a consuming package's own field (e.g. `embedding_model_name` naming a `[models.*]` entry):

```python
from typing import Annotated
from oa_configurator import ConnectionConfig, ModelConfig, RefTo

connection: Annotated[str, RefTo(ConnectionConfig)]
embedding_model_name: Annotated[str, RefTo(ModelConfig)] = "embed-default"
```

`omop-config configure` resolves a `RefTo`-marked field interactively: reuse an existing entry in the target section, or create one on the spot, recursing into any `RefTo` fields the new entry itself has (e.g. a newly-created database recursing into resolving or creating its connection). At load time, `StackConfig` validates that every `RefTo`-marked field resolves to a configured entry, raising a clear error naming the missing section and value otherwise. There is no separate "required"/"owned" declaration list: the field's own type is the declaration, and two packages share an entry simply by both fields resolving to the same name.

---

## Database/Model resolution

`Database`/`Connection` and `Model`/`Provider` are the same two-tier pattern: a mid-tier entry references one leaf-tier entry by name (highlighted below), and `Resolver` resolves the whole pair into one runtime object. Left of each divider is the Python attribute, right is where it lives in `config.toml`. Names below are generic, not this project's real ones.

<iframe src="../diagrams/resource-model-resolution.html" title="Database/Model resolution diagram" style="width: 100%; border: 0; display: block;" loading="lazy"></iframe>
<script>
  (function () {
    var frame = document.currentScript.previousElementSibling;
    function resize() {
      try {
        frame.style.height = frame.contentWindow.document.body.scrollHeight + "px";
      } catch (e) {}
    }
    frame.addEventListener("load", function () {
      resize();
      new ResizeObserver(resize).observe(frame.contentWindow.document.body);
    });
  })();
</script>


---

## Data flow

<iframe src="../diagrams/config-data-flow.html" title="Config data flow diagram" style="width: 100%; border: 0; display: block;" loading="lazy"></iframe>
<script>
  (function () {
    var frame = document.currentScript.previousElementSibling;
    function resize() {
      try {
        frame.style.height = frame.contentWindow.document.body.scrollHeight + "px";
      } catch (e) {}
    }
    frame.addEventListener("load", function () {
      resize();
      new ResizeObserver(resize).observe(frame.contentWindow.document.body);
    });
  })();
</script>

---

## Package integration via entry points

Consuming packages subclass `PackageConfigBase` and register via a `pyproject.toml` entry point:

```toml
[project.entry-points."omop.config"]
my_package = "my_package.config:MyPackageConfig"
```

`omop-config configure my_package` discovers the class at runtime via `importlib.metadata.entry_points(group="omop.config")`, presents the typed fields for interactive configuration, and writes the result to `[tools.my_package]`. `oa-configurator` itself has no knowledge of any consuming package.

---

## Schema translate map

CDM-specific: `ResolvedCDMDatabase.schema_translate_map()` returns the SQLAlchemy-compatible schema translate dict:

```python
{None: "omop", "vocab": "omop_vocab", "results": "results"}
```

OMOP ORM models (omop-alchemy) carry `schema=None` or `schema="vocab"` on their `__table_args__`. The translate map routes them to the correct schema at runtime without changing model definitions. Its keys correspond to the members of [`Role`](api/resources.md#role), the same enum `ResolvedCDMDatabase.connection_target()`/`create_engine()` accept for their `role` parameter. A generic `ResolvedDatabase` has its own, simpler `create_engine()` with no `role` parameter, since a generic entry only ever has one connection.

---

## Security

Passwords are stored in plaintext in `~/.config/omop/config.toml`. Restrict permissions:

```bash
chmod 600 ~/.config/omop/config.toml
```

`ResolvedConnection.safe_url` and `ResolvedConnection.url` are distinct: `safe_url` has the password replaced with `***` and is used for all logging and display. The `.url` value (with plaintext password) is used only for engine creation and never logged by the library.

`RedactingFormatter` (applied by all non-library log presets) scrubs both `key=value` patterns and `://user:password@host` URL patterns from log output.

**Future work**: `secret_source` support (`env:VAR`, `file:path`, Vault, cloud secret managers) is planned but not implemented in this version.

---

## Config path

Default: `~/.config/omop/config.toml`. Override with `OA_CONFIG_PATH=<path/to/config.toml>` (must end in `.toml`; `~` is expanded). Resolved once at module load time and stored as `CONFIG_PATH`.

---

## Future work

- `secret_source` on `ConnectionConfig` (and `ProviderConfig`, for API keys): `env:VARNAME`, `file:PATH`, Vault, cloud secret managers
- Async engine factory (`ResolvedDatabase.create_async_engine()`)
- Project-local overlay (`./oa-config.toml`) layered over user config
