# Migrating from 0.x to 1.0

`oa-configurator` 1.0 is a breaking rewrite of the config schema and the Python/CLI surface. It was done pre-1.0, before any PyPI release depended on the old shape, so nothing here is deprecated first and removed later. It's a clean cutover. This page lists what changed and walks through migrating an existing `~/.config/omop/config.toml` by hand.

---

## Breaking changes at a glance

- **TOML sections renamed and swapped.** `[resources.*]` is now `[databases.*]`; the old `[databases.*]` (raw connections) is now `[connections.*]`. See [TOML migration](#toml-migration) below for the exact field renames that go with this.
- **Profiles removed entirely.** `[profiles.*]`, `active_profile`, `OA_ACTIVE_PROFILE`, and `omop-config use <profile>` no longer exist. Use distinctly-named connections/databases per environment instead (e.g. `cdm_db_prod`, `test_cdm_db`). See [Replacing profiles](#replacing-profiles) below for the full pattern.
- **`default_resource`/resource aliases removed.** A package's `[tools.<name>]` section no longer has a `default_resource` field or participates in an alias dict. Instead, each package declares its own typed field (e.g. `cdm_db`) that names a `[databases.*]` entry directly.
- **`ResourceRef`/`ResourceSpec`/`owned_resources`/`required_resources` removed.** If you maintain a package that integrates with `oa-configurator`, see [Python API changes for package authors](#python-api-changes-for-package-authors) below. This is the biggest change if you have custom `PackageConfigBase` code.
- **CLI commands renamed to match the section swap**: `omop-config databases add/list` (old, connections) → `omop-config connections add/list`; `omop-config resources add/list` (old) → `omop-config databases add/list`.
- **`--resource-name` flag removed.** To point a package at a non-default database, pass the package's own field flag directly (e.g. `--cdm-db cdm_db_prod`), not a generic `--resource-name`.
- **Non-interactive one-shot creation works differently.** `omop-config configure my_pkg --host ... --dialect ...` (flat flags matching the target schema) no longer works.  A package's own `configure` flags now come from the package's own fields, not the target's. Two replacements: create the connection and database first with `connections add`/`databases add`, then point `configure` at them by name; or do it in one call with `--set field.subfield=value` (repeatable, arbitrarily nested), e.g. `configure my_pkg --set cdm_db.connection.dialect=... --set cdm_db.connection.host=...`. See [Integration](integration.md#docker-compose) for both forms.
- **`test_only` is now an ordinary flag** on `connections add` (`--test-only true`, accepts true/false/yes/no/1/0), and via `--set ....test_only=true` when created inline through `configure`.
- **`read_only` removed.** It was never wired to anything (stored, but never read by `oa-configurator` or any consumer) and its description ("hint only") was misleading about that. If you were setting it, it's simply gone. Dropping it from `[connections.*]` is enough.
- **SQLite connections now require an explicit `database_name`.** No more implicit `:memory:` fallback when unset — it now raises at resolve time. See [TOML migration](#toml-migration) step 6.
- **Python API renames**: `resolve_resource()` → `resolve_database()`; old `resolve_database()` (raw connection) → `resolve_connection()`; `ResolvedResource` → `ResolvedDatabase`; old `ResolvedDatabase`/`ResolvedDatabaseTarget` → `ResolvedConnection`; `role="vocab"` string → the `Role` enum (`Role.VOCAB`); pytest plugin's `requires_resource` marker → `requires_database`, `resolve_test_resource` → `resolve_test_database`.
- **`oa_configurator.models` renamed to `oa_configurator.stack_config`.** Only matters if you imported from the submodule directly (`from oa_configurator.models import ...`) instead of the top-level package (`from oa_configurator import ...`). The top-level re-exports are unchanged.
- **`[databases.*]` entries now require an explicit `kind`.** `DatabaseConfig` is no longer one shape: every entry is `kind = "generic"` or `kind = "cdm"` (see [DatabaseKind](api/resources.md#databasekind)), no default, no inference. `cdm_schema` is renamed `schema_name` on both kinds; only the CDM kind still defaults it to `"omop"`. Only `CDMDatabaseConfig` carries `vocab_connection`/`vocab_schema`/`results_schema`. A `RefTo` naming one kind now rejects an entry of the other at construction time.
- **`[vector_stores.*]` is a new section.** Which storage backend an embedding-capable package should use: `backend_type`, a `database` naming a *generic*-kind `[databases.*]` entry, an optional `faiss_cache_dir`, and a free-form `configuration` table. See [Config Reference](config-reference.md#vector_storesname).

---

## TOML migration

Take each section of your existing `config.toml` and apply these renames, in order.

### 1. Rename `[databases.*]` to `[connections.*]`

`dialect`, `host`, `port`, `user`, `password`, `database_name` all keep their names. `read_only` is gone. It was never wired to anything (see above), so drop it if you had it set. `test_only` is new (defaults to `false`; only relevant if you use the [test-database convention](integration.md#integration-tests-a-dedicated-test-database)).

<div class="grid" markdown>

<div markdown>
**Before**

```toml
[databases.cdm]
dialect       = "postgresql+psycopg"
host          = "localhost"
port          = 5432
user          = "omop"
password      = "changeme"
database_name = "omop_cdm"
```
</div>

<div markdown>
**After**

```toml
[connections.cdm]
dialect       = "postgresql+psycopg"
host          = "localhost"
port          = 5432
user          = "omop"
password      = "changeme"
database_name = "omop_cdm"
```
</div>

</div>

### 2. Rename `[resources.*]` to `[databases.*]`, rename its two connection-pointing fields, and add `kind`

`database` → `connection`, `vocab_database` → `vocab_connection`, `cdm_schema` → `schema_name`. `schema_name` used to be required; it now defaults to `"omop"` if omitted, for a `kind = "cdm"` entry specifically (a `kind = "generic"` entry has no such default). Every entry, whether migrated from an old resource or newly added, needs the new `kind` field added explicitly; there is no inference. A resource migrated from `[resources.*]` is always `kind = "cdm"`, since that section only ever held CDM role bundles.

<div class="grid" markdown>

<div markdown>
**Before**

```toml
[resources.cdm]
database       = "cdm"
cdm_schema     = "omop"
vocab_schema   = "omop_vocab"
results_schema = "results"
```
</div>

<div markdown>
**After**

```toml
[databases.cdm]
kind           = "cdm"
connection     = "cdm"
schema_name    = "omop"
vocab_schema   = "omop_vocab"
results_schema = "results"
```
</div>

</div>

If a resource had a separate vocabulary database:

<div class="grid" markdown>

<div markdown>
**Before**

```toml
[resources.cdm]
database       = "cdm"
vocab_database = "central_vocab"
cdm_schema     = "omop"
```
</div>

<div markdown>
**After**

```toml
[databases.cdm]
kind             = "cdm"
connection       = "cdm"
vocab_connection = "central_vocab"
schema_name      = "omop"
```
</div>

</div>

### 3. Delete `[profiles.*]` and `active_profile`

There is no direct TOML equivalent. For each profile you had, create separately-named connections and databases instead, and point each deployment's `omop-config configure` flags at the right ones (see [Replacing profiles](#replacing-profiles)).

### 4. Remove `default_resource` from `[tools.<name>]` sections

Each package's own typed field (e.g. `cdm_db`) now carries this information directly. It will already be present in `[tools.<name>]` if you re-run `omop-config configure <package>` after migrating, or you can add it by hand once you know the field name (check the package's `PackageConfigBase` subclass, or run `omop-config configure <package> --help`).

<div class="grid" markdown>

<div markdown>
**Before**

```toml
[tools.omop_alchemy]
default_resource = "cdm_db"
some_other_field = "..."
```
</div>

<div markdown>
**After**

```toml
[tools.omop_alchemy]
cdm_db = "cdm_db"
some_other_field = "..."
```
</div>

</div>

### 5. `[providers.*]` / `[models.*]`

No change. These sections were introduced alongside this redesign and already use the current shape.

### 6. SQLite connections: `database_name` no longer defaults to `:memory:`

A `[connections.*]` entry with `dialect = "sqlite"` used to fall back to `:memory:` when `database_name` was left unset. It now raises at resolve time instead. Only entries that relied on the old implicit default need editing; anything that already set `database_name` explicitly renders to exactly the same URL as before, no change needed.

<div class="grid" markdown>

<div markdown>
**Before**

```toml
[connections.scratch]
dialect = "sqlite"
```
</div>

<div markdown>
**After**

```toml
[connections.scratch]
dialect       = "sqlite"
database_name = ":memory:"
```
</div>

</div>

---

## Replacing profiles

Profiles let you switch a whole environment (dev/test/prod) by flipping one name. The replacement is naming convention, applied per environment instead of per profile:

<div class="grid" markdown>

<div markdown>
**Before** (one `cdm` connection, overridden per profile)

```toml
[databases.cdm]
host = "prod.example.com"
...

[profiles.test.databases.cdm]
host = "localhost"
...
```
</div>

<div markdown>
**After** (two independently-named connections, no profile switch)

```toml
[connections.cdm]
host = "prod.example.com"
...

[connections.test_cdm]
host = "localhost"
...

[databases.cdm_db]
connection = "cdm"

[databases.test_cdm_db]
connection = "test_cdm"
```
</div>

</div>

Application code that used to rely on the active profile now names the database it wants explicitly (`resolve_database("cdm_db")` vs. `resolve_database("test_cdm_db")`), or a package's own field points at whichever one it should use by default, overridable per deployment with that field's own CLI flag.

---

## Python API changes for package authors

If your package has its own `PackageConfigBase` subclass, the biggest change is how it declares which database/model it needs.

<div class="grid" markdown>

<div markdown>
**Before**

```python
from oa_configurator import PackageConfigBase, ResourceRef, ResourceSpec

class MyPackageConfig(PackageConfigBase):
    tool_name = "my_package"
    owned_resources = (ResourceSpec(semantic_name="cdm_db", ...),)
```
</div>

<div markdown>
**After**

```python
from typing import Annotated, ClassVar
from oa_configurator import CDMDatabaseConfig, PackageConfigBase, RefTo

class MyPackageConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "my_package"
    cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"
```
</div>

</div>

There is no equivalent of `required_resources`/`ResourceRef` for consuming a *different* package's database. Declare a field with the same `RefTo(CDMDatabaseConfig)` type and the same default name as the owning package's field. The two packages share the entry simply because both fields resolve to the same name. See [Integration](integration.md#cross-package-database-references).

Engine creation: replace `resolver.resolve_resource(name).create_engine()` with `resolver.resolve_database(name).create_engine()`. For the vocabulary connection, replace `create_engine(role="vocab")` with `create_engine(role=Role.VOCAB)` (`from oa_configurator import Role`).

Tests using the pytest plugin: replace `@pytest.mark.requires_resource(...)` with `@pytest.mark.requires_database(...)`, and `resolve_test_resource(...)` with `resolve_test_database(...)`.

Full current shape: [Integration](integration.md), [Config Reference](config-reference.md), [Architecture](architecture.md).
