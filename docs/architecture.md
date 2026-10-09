# Architecture

## Purpose

`oa-configurator` is a shared configuration layer for the OMOP-oriented Python stack. It replaces per-package `.env` files, inconsistent env var lookups, and duplicated engine-creation boilerplate with a single typed TOML file and a common resolver interface.

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

Every `[databases.<name>]` entry declares an explicit `kind` (see [DatabaseKind](api/resources.md#databasekind) for the current members), no default, no inference. The kind decides which concrete fields exist on top of the shared `connection` base (see [Resources](api/resources.md#genericdatabaseconfig) for the field list each kind adds).

  - `kind=GENERIC`
    - `schema_name`: The schema in which all tables sit
  - `kind=CDM`
    - `cdm_schema`: The schema of all CDM tables.
    - `vocab_schema`: The schema of all vocabulary tables within the CDM.
    - `results_schema`: The schema of all results tables within the CDM.

Neither has a default on its kind (unset means "use the connection's own default"), and both are rejected at construction time if set against a connection whose dialect has no real schema concept (e.g. SQLite).

```toml
[databases.emb_db]
kind       = "generic"
connection = "emb"

[databases.cdm_db]
kind       = "cdm"
connection = "cdm"
cdm_schema = "omop"
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

Per-package configuration lives in `[tools.<name>]`. `StackConfig` keeps these sections as plain dictionaries because oa-configurator discovers package schemas at runtime, but that does not make them unchecked: before an official configure flow saves a change, it loads the package's `PackageConfigBase` subclass and validates both the section and its `RefTo` fields against the proposed stack.

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

Two packages point their own `RefTo`-marked field at the same entry non-interactively by naming it directly, e.g. `omop-config configure <package> --set cdm_db=<existing-name>`; no need to reconfigure the connection or schema a second time.

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

`omop-config configure my_package` discovers the class at runtime via `importlib.metadata.entry_points(group="omop.config")`, uses its typed fields to guide the user, and saves the validated result to `[tools.my_package]`. This lets oa-configurator support package-specific configuration without building knowledge of each consuming package into the core library.

An application with its own configuration UI uses `plan_configure()` to get the same package-aware behaviour without file I/O. It receives a new complete `StackConfig`, can present that proposal for review, and decides whether to pass it to `save_stack_config()`. Frontends therefore do not need to reproduce package schemas or `RefTo` traversal.

---

## Schema translate map

CDM-specific: `ResolvedCDMDatabase.configured_internal_schema_translate_map()` returns the SQLAlchemy-compatible schema translate dict:

```python
{"primary": "omop", "vocab": "omop_vocab", "results": "results"}
```

OMOP ORM models carry `schema="primary"`, `schema="vocab"` or `schema="results"` on their `__table_args__`. The translate map routes them to the correct schema at runtime without changing model definitions. Its keys correspond to the members of [`Role`](api/resources.md#role), the same enum `ResolvedCDMDatabase.connection_for_role()`/`create_engine()` accept for their `role` parameter. 

!!! note "Untagged table"
    A genuinely untagged table (no `schema` set at all in `__table_args__`) is not part of this routing and falls through to the connection's own default/`search_path`

`create_engine()`'s own `schema_translate_map` is authoritative, not a default:

- an `execution_options` argument may *extend* the map with a key the resolver doesn't own (e.g. a package's own reserved-schema role, layered on top of the CDM map, see [Vector Stores](api/vector-stores.md) for a real example), 
- supplying protected schemas raises `ValueError` rather than silently overriding the configured routing.

---

## Schema provenance guard { #schema-provenance-guard }

`schema_translate_map()` resolves a table's *current* physical schema correctly, but on its own gives no memory of a table's *previous* one. If a role's configured schema changes between two runs (a typo, an incomplete migration, two configs drifting apart), nothing would otherwise stop `create_all()` from silently creating a second, orphaned copy of the tables under the new schema while the old copy sits there unnoticed.

**Enforcement happens at engine construction** (`create_engine()`, or `create_engines()` for a CDM database). A connection's `schema_translate_map` is fixed for its lifetime, so one check at construction covers every read/DML/DDL done through it. Every claim engine construction registers, the schema-registry table's own reservation plus each configured `Role`'s schema, is recorded as a baseline row in a small bookkeeping table (`schema_registry`, its own reserved schema), and a *mismatching* existing row raises `SchemaDriftError` immediately, whether `register_claims=True` (a real write) or `False` (a read-only/diagnostic engine checks and raises too, with nothing written). `guard_schema_provenance_for(connection, *, schema_tag)` (`schema.py`) remains for the case of a long-lived engine with a config that may have been altered between the creation and the DDL about to run:

```python
with guard_schema_provenance_for(connection, schema_tag=Role.VOCAB):
    Base.metadata.create_all(bind=connection, tables=vocab_tables, checkfirst=True)
```

`connection` must have been built by `create_engine()` or `create_engines()`, which stash the entry name and `test_only` in `execution_options` alongside `schema_translate_map` for the guard to read back. Everywhere else, a short-lived engine rebuilt per call (the common case: any CLI command, any library function taking an engine or session per invocation) is re-validated automatically on every build and needs no explicit guard at all.

Rows are keyed by the physical database (host, port and database name) **and**, for a `Role` tag (`primary`/`vocab`/`results`), the `database_config_name` that established it: two different `[databases.*]`/`[vector_stores.*]` entries may each claim the same Role tag on one connection (a CDM database and its own colocated vector store, say) without colliding, since "has *this entry's* mapping changed" is what the row tracks, not "has *this connection's* mapping changed." A custom (non-Role) tag stays scoped per connection only: one claim per tag per connection, owner-checked. See [Config-time entry exclusivity](#entry-exclusivity) for the complementary config-level check that keeps this scoping meaningful rather than something two entries stumble into by accident.

`test_only=True` re-baselines a mismatching row instead of raising (a test-only connection's schema is expected to change between runs, and has no production data worth protecting) and bypasses the "already has tables, no baseline" check on first registration for a Role tag. A custom claim's first registration into a populated schema never raises regardless of `test_only`: it's declared by the owning package's own code, so there's nothing else that could ever "adopt" it, and the config has no visibility into what its baseline should be anyway. A registry predating `connection_key` entirely raises `SchemaRegistryOutdatedError`. `find_table_in_other_schemas()` complements the guard directly for drift that predates the bookkeeping table entirely, checking the database's actual physical layout rather than a stored claim.

oa-configurator owns the guard, the bookkeeping table, and the CLI-level remediation path for a genuine migration, generic over any `[databases.*]` entry rather than tied to any particular domain package:

- `omop-config verify` reports drift per configured database/role, and additionally lists every raw `schema_registry` row for each configured connection (including a package's own custom tag and any stale row left behind by a renamed or removed entry). This is a pure read, with no enforcement of its own; a custom tag's drift is still only enforced at the point its owning package calls `create_engine()` for it.
- `omop-config acknowledge-schema-migration --database <name> --reason <text> [--schema-tag <tag>] [--new-schema <schema>]` records a schema as the deliberate new baseline, `--database` as the entry owning the mapping. For a Role tag, `--new-schema` is optional: omit it and the baseline resolves fresh from the current config (whatever's configured becomes the new baseline); pass it only to override that. Omit `--schema-tag` entirely to acknowledge every Role tag of the entry at once (also the only way to baseline an *existing* database since a first-registration into a populated schema otherwise raises). For a custom tag, `--new-schema` is required: its physical schema lives only in the owning package's own `SchemaClaim`, so there's nothing to resolve it from.
- `omop-config release-schema-claim --database <name> --schema-tag <tag> [--role <role>] [--confirm]` deletes a stale row with no other remedy (a renamed entry's old row blocking re-acknowledgment, or a custom tag's row with no config-derivable value to re-acknowledge to). Previews only, unless `--confirm` is given.
- `omop-config drop-orphan-schema-tables --database <name> --schema <schema> [--role <role>] [--confirm] [--allow-default-schema]` drops tables physically found in an orphaned schema, after checking the named schema isn't still the current target of any configured database/role. The DROP is emitted as literal, qualified SQL per table, instead of `metadata.drop_all()` may drop the wrong table (the DDL compiler consults the connection's `schema_translate_map`, so an orphan schema whose name happens to equal a tag key [e.g. a legacy physical schema literally named `vocab`] would otherwise be silently translated to the *configured* physical schema and drop the wrong data). Previews only, unless `--confirm` is given; refuses a dialect system schema or the dialect's own default schema unless `--allow-default-schema` is passed.

!!! note
    None of these commands move data automatically. Resolving a genuine migration is always an explicit, operator-run action with its own reasoning recorded.

---

## Config-time entry exclusivity { #entry-exclusivity }

`StackConfig` validates at load time that no two independent entries can be configured against the same physical connection in a way that would make the schema registry's per-entry scoping above load-bearing in the first place:

- No two `[connections.*]` entries may describe the same `(dialect, host, port, database_name, user)` identity
    - differing only by `user` (e.g. a low-privilege app role vs. an admin role against the same server) is a legitimate, distinct connection. 
    - **Exceptions**:
        - `test_only` connections: tests routinely clone the one real test server under several connection names purely to exercise multi-connection routing, with no need for genuine physical separation and 
        - any genuinely ephemeral SQLite URL: two engines on the same `:memory:` URL are, by SQLAlchemy's own semantics, disconnected databases, not the same one.
- No two CDM database entries, and no two vector-store entries, may share a primary-connection identity
    - Deliberately not cross-checked against each other: a vector store's backing database sharing a connection with a CDM entry is the "extend this CDM's database with embedding storage" pattern, and must stay legal.
- Several CDM entries sharing one `vocab_connection` is explicitly allowed and unchecked
    - the standard OMOP pattern: one shared vocabulary reused across many CDM instances
    - only each entry's own primary connection participates in the exclusivity check above.
- A CDM's `vocab_connection` physically coinciding with *another* entry's primary connection is allowed but logs a warning
    - most likely a copy-paste/typo, to flag without forbidding a deliberate choice.
    - the registry's per-entry keying keeps it drift-safe

!!! note
    - `connection_key()` itself remains a pure function of the *configured* URL (host, port, database name). The same physical database reached through two differently-spelled routes (e.g. hostname vs. IP) is not detected as a collision anywhere in this stack. 
    - Configure each physical database through one canonical, consistently-spelled connection across every `[connections.*]` entry that targets it.

---

## Dialect support across the stack

[`Dialect`](api/resources.md#dialect) (`sql.py`, next to `Role`) is the one canonical enum naming the SQLAlchemy backends this codebase recognizes: `postgresql` and `sqlite` today. It sits in oa-configurator because it's needed at the bottom of the dependency graph, below every package that dispatches on it.

`Dialect` itself carries no behavior and makes no claim that a given member is supported anywhere in particular. Each consuming package is expected to maintain its own `Dialect`-keyed dispatch registry (a dict from `Dialect` to that package's own implementation), raising a clear, named error for anything unregistered rather than silently misrouting or crashing unhelpfully. A caller with an arbitrary engine and an unknown dialect should always go through that package's own factory function rather than instantiating a concrete backend class directly, so an unsupported dialect fails loudly and consistently in one place.

**To add a new dialect**: add it to `Dialect` here, then register it in whichever consuming package's own dispatch registry actually needs it.

---

## Security

Passwords are stored in plaintext in `~/.config/omop/config.toml`. Restrict permissions:

```bash
chmod 600 ~/.config/omop/config.toml
```

`ResolvedConnection.safe_url` and `ResolvedConnection.url` are distinct: `safe_url` has the password replaced with `***` and is used for all logging and display. The `.url` value (with plaintext password) is used only for engine creation and never logged by the library.

`RedactingFilter` (installed by `configure_logging()`) scrubs both `key=value` patterns and `://user:password@host` URL patterns from log output.

`save_stack_config()` validates and serializes before changing the destination, protects candidate and backup files before writing credentials, and verifies the result after an atomic replacement. See [Saving configuration safely](api/persistence.md) for recovery behaviour. Atomic replacement protects file integrity but does not coordinate concurrent writers, which remain last-writer-wins in this release.

**Future work**: `secret_source` support (`env:VAR`, `file:path`, Vault, cloud secret managers) is planned but not implemented in this version.

---

## Config path

Default: `~/.config/omop/config.toml`. Override with `OA_CONFIG_PATH=<path/to/config.toml>` (must end in `.toml`; `~` is expanded). Resolved once, at module load time. Call `active_config_path()` to read it; the raw `CONFIG_PATH` module attribute is internal to `loader.py`.

---

## Future work

- `secret_source` on `ConnectionConfig` (and `ProviderConfig`, for API keys): `env:VARNAME`, `file:PATH`, Vault, cloud secret managers
- Async engine factory (`ResolvedDatabase.create_async_engine()`)
- Project-local overlay (`./oa-config.toml`) layered over user config
