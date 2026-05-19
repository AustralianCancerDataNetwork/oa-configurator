# Architecture

## Purpose

This package is a shared configuration library for the stack around:

- `omop-alchemy`
- `orm-loader`
- `omop-emb`
- `omop-graph`
- related tools that need databases, schemas, and local artifact roots

It is meant to replace the current pattern of:

- package-local env var lookups
- dotenv-heavy setup
- incompatible config idioms across repos

## Core Model

The prototype is organized around four ideas:

1. `profile`
   Which environment is active, for example `local`, `staging`, or `prod`.

2. `connection`
   A concrete database or service endpoint.

3. `resource`
   A logical role mapping, for example:
   - primary OMOP/CDM database
   - vocab database
   - results database
   - local DuckDB export root
   - local FAISS root

4. `tool`
   Tool-specific defaults, for example:
   - `omop_emb` backend and default resource
   - `orm_loader` database-file export root
   - `omop_graph` default resource

## Why This Shape

This package is trying to keep two important truths in view at the same time:

- humans want one obvious place to manage configuration
- code wants typed, explicit, role-based resolved settings

That means config must answer both:

1. What infrastructure do I have?
2. How does a given tool or workflow map onto it?

## Filesystem Path Convention

Relative filesystem paths should never be interpreted relative to the process
working directory.

That gets too confusing too quickly once:

- multiple runtimes read the same file
- notebooks and CLIs run from different directories
- background jobs and services resolve the same configuration in parallel

The convention is:

- `settings.configuration_base_path = "."` means "use the fully resolved
  directory containing this configuration file"
- any other `configuration_base_path` must be an absolute directory path
- every other filesystem location in the config is then either:
  - absolute
  - or relative to `configuration_base_path`

Path semantics should be easy to explain and predictable across runtimes.

### Example

```toml
[settings]
active_profile = "local"
configuration_base_path = "."

[resources.default]
artifact_root = "artifacts"
embedding_file_root = "artifacts/embeddings"
analytic_db_file_root = "artifacts/databases"
athena_source_path = "/srv/athena"
```

If the config file lives at:

```text
/home/alice/.config/omop/config.toml
```

then these resolve to:

- `artifact_root` -> `/home/alice/.config/omop/artifacts`
- `embedding_file_root` -> `/home/alice/.config/omop/artifacts/embeddings`
- `analytic_db_file_root` -> `/home/alice/.config/omop/artifacts/databases`
- `athena_source_path` -> `/srv/athena`

## Secret Sources

Inline passwords are still allowed for local or throwaway setups, but the
configuration model now supports indirect secret lookup as well.

- `settings.secrets_dir` is an optional filesystem root for file-backed
  secrets
- `connections.<name>.secret_source` is an explicit source string
- `password` and `secret_source` are mutually exclusive on a connection

The sketch currently supports two secret source formats:

- `env:VARIABLE_NAME`
- `file:relative/or/absolute/path`

Relative `file:` sources resolve from `settings.secrets_dir` when that setting
is present. Otherwise they resolve from `configuration_base_path`.

### Example

```toml
[settings]
active_profile = "prod"
configuration_base_path = "."
secrets_dir = "secrets"

[connections.prod_cdm]
dialect = "postgresql"
host = "prod.hospital.org"
port = 5432
user = "omop_prod"
secret_source = "file:prod_cdm.password"
database = "omop_cdm"

[connections.prod_vocab]
dialect = "postgresql"
host = "prod.hospital.org"
port = 5432
user = "omop_vocab"
secret_source = "env:OA_PROD_VOCAB_PASSWORD"
database = "omop_vocab"
```

## Profile Overlay Shape

Profiles now do more than act as labels.

Right now a profile is mostly metadata plus a selected name:

- `local`
- `staging`
- `prod`

The current version allows a profile to act as a structured patch over the
base configuration. The goal is to avoid copying entire resource or tool blocks
just because one or two values change between environments.

### Design Intent

The base file should define the stable logical shape:

- named connections
- named resources
- named tool defaults

Profiles should then override only the parts that differ for a specific
environment, such as:

- which connection a resource points at
- schema names
- local artifact roots
- tool backends
- tool storage roots
- read-only toggles

### TOML Shape

This is the shape the prototype now supports:

```toml
[settings]
active_profile = "local"
configuration_base_path = "."
secrets_dir = "secrets"

[connections.local_cdm]
dialect = "postgresql"
host = "localhost"
port = 5432
user = "omop"
password = "omop"
database = "omop"

[connections.prod_cdm]
dialect = "postgresql"
host = "prod.hospital.org"
port = 5432
user = "omop_prod"
secret_source = "file:prod_cdm.password"
database = "omop_cdm"

[connections.prod_vocab]
dialect = "postgresql"
host = "prod.hospital.org"
port = 5432
user = "omop_vocab"
secret_source = "file:prod_vocab.password"
database = "omop_vocab"

[resources.default]
primary_db = "local_cdm"
vocab_db = "local_cdm"
results_db = "local_cdm"
omop_schema = "cdm"
vocab_schema = "cdm"
results_schema = "results"
artifact_root = "~/.local/share/omop-local"
embedding_file_root = "~/.local/share/omop-local/embeddings"
analytic_db_file_root = "~/.local/share/omop-local/databases"

[tools.omop_emb]
default_resource = "default"
backend = "pgvector"
embedding_file_root = "~/.local/share/omop-local/embeddings"

[profiles.prod]
description = "remote OMOP source with local derived artifacts"

[profiles.prod.resource_overrides.default]
primary_db = "prod_cdm"
vocab_db = "prod_vocab"
results_db = "prod_cdm"
vocab_schema = "vocab"
artifact_root = "~/.local/share/omop-prod"
embedding_file_root = "~/.local/share/omop-prod/embeddings"
analytic_db_file_root = "~/.local/share/omop-prod/databases"

[profiles.prod.tool_overrides.omop_emb]
embedding_file_root = "~/.local/share/omop-prod/embeddings"

[profiles.prod.tool_overrides.orm_loader]
database_file_root = "~/.local/share/omop-prod/databases"
```

### Merge Semantics

The intended merge rules should be simple and explicit:

1. load the base config
2. select the active profile
3. apply `resource_overrides` by resource name
4. apply `tool_overrides` by tool name
5. optionally apply `connection_overrides` if we decide that is necessary

This is implemented as a shallow field-level merge for each named object, not a
free-form recursive patch language.

### Why This Is Better Than Duplicating Resource Blocks

Without overlays, users will quickly end up with:

- `default_local`
- `default_prod`
- `default_staging`
- `default_ci`

and then every tool has to know which duplicated resource to select.

With overlays:

- the logical resource name stays stable
- the active profile changes the wiring beneath it
- code can keep asking for `default`

That is the important ergonomic win.

### What Should Probably Be Overridable

Strong candidates:

- `ResourceConfig` fields
- `ToolConfig` fields

Possible but more debatable:

- selected `ConnectionConfig` fields, for example host or database name

General considerations:

- prefer overriding resource and tool mappings first
- only add connection-level overlays if there is a real repeated need

That keeps the mental model simpler.

## Implemented

- Typed Pydantic v2 models for all config sections
- TOML loading with environment-variable overrides (`OA_CONFIG_FILE`, `OA_ACTIVE_PROFILE`)
- Cross-reference validation at load time (unknown connection/resource names raise immediately)
- Resolver: logical names → typed, secret-resolved, path-expanded handles
- Profile overlays: shallow field-level merge for resources and tools
- Secret sources: `env:VARNAME` and `file:path`
- `ResolvedDatabaseTarget.create_engine()` and `ResolvedResource.create_engine()`
- `ResolvedResource.schema_translate_map()` — SQLAlchemy `{None: omop_schema, "vocab": vocab_schema, ...}`
- `ResolvedResource.vocab_db_is_primary_fallback` / `results_db_is_primary_fallback`
- `StackConfig.for_session()` — inline construction without a TOML file
- `Resolver.with_overrides()` — session-level connection/resource replacement
- Interactive REPL namespaces with tab completion (`resolver.resources.default`)
- CLI wizards: `add-connection`, `add-resource`, `add-profile`, `show`, `resolve-resource`, `resolve-tool`
- TOML persistence (`save_stack_config`)

## Possible future additions

- Doctor / validation command that checks connectivity and schema presence
- Project-local overlay file (`./oa-config.toml`) that merges over the user config
- Compatibility shims that export resolved config back to legacy env var format
- Vault / cloud secrets manager sources beyond `env:` and `file:`
- Async engine factory for asyncpg / greenlet-based stacks

## Design notes

`active_stack` was considered as a way to select a default resource by name but
was removed — `active_profile` + resource naming covers the use case without
the extra indirection.

`schema_translate_map` follows the SQLAlchemy convention of mapping `None` to
the unqualified (default) schema. OMOP clinical tables carry `schema=None` in
the ORM; the translate map routes them to the configured OMOP schema at runtime
without changing model definitions.

Inline secrets (`password = "..."`) are allowed but not recommended for shared
config files. The `secret_source` mechanism keeps the TOML file safe to commit.
