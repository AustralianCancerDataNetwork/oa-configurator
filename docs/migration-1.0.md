# Migrating from 0.x to 1.0

`oa-configurator` 1.0 is a breaking rewrite of the config schema and the Python/CLI surface. It was done pre-1.0, before any PyPI release depended on the old shape, so nothing here is deprecated first and removed later — it's a clean cutover. This page lists what changed and walks through migrating an existing `~/.config/omop/config.toml` by hand.

---

## Breaking changes at a glance

- **TOML sections renamed and swapped.** `[resources.*]` is now `[databases.*]`; the old `[databases.*]` (raw connections) is now `[connections.*]`. See [TOML migration](#toml-migration) below for the exact field renames that go with this.
- **Profiles removed entirely.** `[profiles.*]`, `active_profile`, `OA_ACTIVE_PROFILE`, and `omop-config use <profile>` no longer exist. Use distinctly-named connections/databases per environment instead (e.g. `cdm_db_prod`, `test_cdm_db`) — see [Replacing profiles](#replacing-profiles) below.
- **`default_resource`/resource aliases removed.** A package's `[tools.<name>]` section no longer has a `default_resource` field or participates in an alias dict. Instead, each package declares its own typed field (e.g. `cdm_db`) that names a `[databases.*]` entry directly.
- **`ResourceRef`/`ResourceSpec`/`owned_resources`/`required_resources` removed.** If you maintain a package that integrates with `oa-configurator`, see [Python API changes for package authors](#python-api-changes-for-package-authors) below — this is the biggest change if you have custom `PackageConfigBase` code.
- **CLI commands renamed to match the section swap**: `omop-config databases add/list` (old, connections) → `omop-config connections add/list`; `omop-config resources add/list` (old) → `omop-config databases add/list`.
- **`--resource-name` flag removed.** To point a package at a non-default database, pass the package's own field flag directly (e.g. `--cdm-db cdm_db_prod`), not a generic `--resource-name`.
- **Non-interactive one-shot creation no longer works.** `omop-config configure my_pkg --host ... --dialect ...` used to create a connection and database in one call. It doesn't anymore — non-interactive `configure` now only points at already-existing entries. Create them first with `connections add`/`databases add`, then point `configure` at them. See [Integration](integration.md#docker-compose) for the current scripted flow.
- **`test_only` still can't be set non-interactively.** There is currently no `--test-only` flag on `connections add`. Use the interactive `configure <package>` flow to create a marked test connection, or hand-edit `test_only = true` into the TOML after creating it non-interactively. Tracked as a known gap, not a design decision.
- **Python API renames**: `resolve_resource()` → `resolve_database()`; old `resolve_database()` (raw connection) → `resolve_connection()`; `ResolvedResource` → `ResolvedDatabase`; old `ResolvedDatabase`/`ResolvedDatabaseTarget` → `ResolvedConnection`; `role="vocab"` string → the `Role` enum (`Role.VOCAB`); pytest plugin's `requires_resource` marker → `requires_database`, `resolve_test_resource` → `resolve_test_database`.
- **`oa_configurator.models` renamed to `oa_configurator.stack_config`.** Only matters if you imported from the submodule directly (`from oa_configurator.models import ...`) instead of the top-level package (`from oa_configurator import ...`). The top-level re-exports are unchanged.

---

## TOML migration

Take each section of your existing `config.toml` and apply these renames, in order.

### 1. Rename `[databases.*]` to `[connections.*]`

No field changes — `dialect`, `host`, `port`, `user`, `password`, `database_name`, `read_only` all keep their names. `test_only` is new (defaults to `false`; only relevant if you use the [test-database convention](integration.md#integration-tests-a-dedicated-test-database)).

```diff
-[databases.cdm]
+[connections.cdm]
 dialect       = "postgresql+psycopg"
 host          = "localhost"
 port          = 5432
 user          = "omop"
 password      = "changeme"
 database_name = "omop_cdm"
```

### 2. Rename `[resources.*]` to `[databases.*]`, and rename its two connection-pointing fields

`database` → `connection`, `vocab_database` → `vocab_connection`. `cdm_schema` used to be required; it now defaults to `"omop"` if omitted.

```diff
-[resources.cdm]
-database       = "cdm"
+[databases.cdm]
+connection     = "cdm"
 cdm_schema     = "omop"
 vocab_schema   = "omop_vocab"
 results_schema = "results"
```

If a resource had a separate vocabulary database:

```diff
-[resources.cdm]
-database        = "cdm"
-vocab_database  = "central_vocab"
+[databases.cdm]
+connection       = "cdm"
+vocab_connection = "central_vocab"
 cdm_schema      = "omop"
```

### 3. Delete `[profiles.*]` and `active_profile`

There is no direct TOML equivalent. For each profile you had, create separately-named connections and databases instead, and point each deployment's `omop-config configure` flags at the right ones (see [Replacing profiles](#replacing-profiles)).

### 4. Remove `default_resource` from `[tools.<name>]` sections

Each package's own typed field (e.g. `cdm_db`) now carries this information directly — it will already be present in `[tools.<name>]` if you re-run `omop-config configure <package>` after migrating, or you can add it by hand once you know the field name (check the package's `PackageConfigBase` subclass, or run `omop-config configure <package> --help`).

```diff
 [tools.omop_alchemy]
-default_resource = "cdm_db"
+cdm_db = "cdm_db"
 some_other_field = "..."
```

### 5. `[providers.*]` / `[models.*]`

No change — these sections were introduced alongside this redesign and already use the current shape.

---

## Replacing profiles

Profiles let you switch a whole environment (dev/test/prod) by flipping one name. The replacement is naming convention, applied per environment instead of per profile:

**Before** (one `cdm` connection, overridden per profile):

```toml
[databases.cdm]
host = "prod.example.com"
...

[profiles.test.databases.cdm]
host = "localhost"
...
```

**After** (two independently-named connections, no profile switch):

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

Application code that used to rely on the active profile now names the database it wants explicitly (`resolve_database("cdm_db")` vs. `resolve_database("test_cdm_db")`), or a package's own field points at whichever one it should use by default, overridable per deployment with that field's own CLI flag.

---

## Python API changes for package authors

If your package has its own `PackageConfigBase` subclass, the biggest change is how it declares which database/model it needs.

**Before**:

```python
from oa_configurator import PackageConfigBase, ResourceRef, ResourceSpec

class MyPackageConfig(PackageConfigBase):
    tool_name = "my_package"
    owned_resources = (ResourceSpec(semantic_name="cdm_db", ...),)
```

**After**:

```python
from typing import Annotated, ClassVar
from oa_configurator import DatabaseConfig, PackageConfigBase, RefTo

class MyPackageConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "my_package"
    cdm_db: Annotated[str, RefTo(DatabaseConfig)] = "cdm_db"
```

There is no equivalent of `required_resources`/`ResourceRef` for consuming a *different* package's database. Declare a field with the same `RefTo(DatabaseConfig)` type and the same default name as the owning package's field — the two packages share the entry simply because both fields resolve to the same name. See [Integration](integration.md#cross-package-database-references).

Engine creation: replace `resolver.resolve_resource(name).create_engine()` with `resolver.resolve_database(name).create_engine()`. For the vocabulary connection, replace `create_engine(role="vocab")` with `create_engine(role=Role.VOCAB)` (`from oa_configurator import Role`).

Tests using the pytest plugin: replace `@pytest.mark.requires_resource(...)` with `@pytest.mark.requires_database(...)`, and `resolve_test_resource(...)` with `resolve_test_database(...)`.

Full current shape: [Integration](integration.md), [Config Reference](config-reference.md), [Architecture](architecture.md).
