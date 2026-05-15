# Architecture

## Purpose

This package is a shared configuration library for the stack around:

- `OMOP_Alchemy`
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
password = "secret"
database = "omop_cdm"

[connections.prod_vocab]
dialect = "postgresql"
host = "prod.hospital.org"
port = 5432
user = "omop_vocab"
password = "secret"
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
- selected global settings like `active_stack`

Possible but more debatable:

- selected `ConnectionConfig` fields, for example host or database name

My current bias remains:

- prefer overriding resource and tool mappings first
- only add connection-level overlays if there is a real repeated need

That keeps the mental model simpler.

## Intended Evolution

The current prototype implements:

- typed models
- TOML loading
- simple resolution
- CLI skeleton

Still to add:

- compatibility exporters for legacy env vars
- a doctor/validation command
- secrets-dir and secret-source support
- project-local overlay file support
- more complete tool-specific resolution helpers
