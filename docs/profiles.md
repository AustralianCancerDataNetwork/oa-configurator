# Profiles & Overlays

Profiles let you maintain a single config file for multiple environments (local, staging, prod) without duplicating entire resource or tool blocks.

---

## Concept

The **base config** defines the stable logical shape: named connections, resources, and tools.

A **profile** is a named set of patches applied on top at resolution time. Only the fields that differ between environments need to appear in the profile — everything else stays as defined in the base.

```
base config  →  profile overlay  →  resolved resource
```

The merge is a shallow field-level replacement: each field in the override replaces the corresponding base field independently.

---

## Selecting the active profile

In the config file:

```toml
[settings]
active_profile = "local"
```

Via environment variable (overrides the file):

```bash
OA_ACTIVE_PROFILE=prod python my_script.py
```

---

## Profile structure

```toml
[profiles.<name>]
description = "optional human-readable label"

[profiles.<name>.resource_overrides.<resource_name>]
# any subset of ResourceConfig fields

[profiles.<name>.tool_overrides.<tool_name>]
# any subset of ToolConfig fields
```

---

## Worked example

### Base config

```toml
[settings]
active_profile = "local"

[connections.local_cdm]
dialect  = "postgresql"
host     = "localhost"
database = "omop"
user     = "omop"
password = "omop"

[connections.prod_cdm]
dialect       = "postgresql"
host          = "prod.hospital.org"
database      = "omop_cdm"
user          = "omop_prod"
secret_source = "file:prod_cdm.password"

[connections.prod_vocab]
dialect       = "postgresql"
host          = "prod.hospital.org"
database      = "omop_vocab"
user          = "omop_vocab"
secret_source = "file:prod_vocab.password"

[resources.default]
primary_db            = "local_cdm"
vocab_db              = "local_cdm"
results_db            = "local_cdm"
omop_schema           = "cdm"
vocab_schema          = "cdm"
results_schema        = "results"
artifact_root         = "artifacts/local"
embedding_file_root   = "artifacts/local/embeddings"

[tools.omop_emb]
default_resource    = "default"
backend             = "pgvector"
embedding_file_root = "artifacts/local/embeddings"
```

### Profile patch

```toml
[profiles.prod]
description = "remote OMOP source, local derived artifacts"

[profiles.prod.resource_overrides.default]
primary_db            = "prod_cdm"
vocab_db              = "prod_vocab"
results_db            = "prod_cdm"
vocab_schema          = "vocab"
artifact_root         = "artifacts/prod"
embedding_file_root   = "artifacts/prod/embeddings"

[profiles.prod.tool_overrides.omop_emb]
embedding_file_root = "artifacts/prod/embeddings"
```

When `active_profile = "prod"`:

- `resource.primary_db` → `prod_cdm` (changed)
- `resource.vocab_db` → `prod_vocab` (changed)
- `resource.omop_schema` → `"cdm"` (unchanged, from base)
- `resource.vocab_schema` → `"vocab"` (changed)
- `tool.embedding_file_root` → `"artifacts/prod/embeddings"` (changed)

---

## Merge semantics

Profile overrides are applied as a **shallow field-level merge**:

1. Start with the base resource (or tool) config.
2. For each field present in the override, replace the base field value.
3. Fields absent from the override keep their base values.
4. `tool.extra` is merged shallowly: override keys are added or replaced, base keys not present in the override are kept.

This is not a recursive patch language. It is intentionally simple.

---

## Why not duplicate resource blocks?

Without profiles, handling multiple environments leads to:

```toml
[resources.default_local]
[resources.default_staging]
[resources.default_prod]
```

Every tool then has to know which variant to use. With profiles the logical name `default` stays stable — only the wiring beneath it changes based on the active profile.
