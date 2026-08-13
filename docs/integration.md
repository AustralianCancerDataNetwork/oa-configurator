# Integration

!!! info
    This guide showcases how to make your package support `omop-config configure <package>` and use `oa-configurator` for engine creation and logging.

---

## Overview

Each package that integrates with `oa-configurator`:

1. Subclasses `PackageConfigBase` with its typed config fields
2. Registers the class via an entry point in `pyproject.toml`
3. Calls `MyPackageConfig.get_config()` to read its config
4. Uses `Resolver.from_active_config().resolve_database("cdm").create_engine()` for SQLAlchemy
5. Calls `configure_logging(verbosity=verbose, extra_namespaces=["<package>"])` at startup

---
## Steps for integration

### 1: Add the dependency

In your `pyproject.toml`:

```toml
[project.dependencies]
"oa-configurator>=0.2.0"  # version may vary
```

---

### 2. Define your config class

In `src/<package>/config.py`:

```python
from typing import Annotated, ClassVar
from pydantic import Field
from oa_configurator import CDMDatabaseConfig, PackageConfigBase, RefTo


class MyPackageConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "my_package"   # maps to [tools.my_package] in TOML

    # A field naming an entry in [databases.*] of kind "cdm". `omop-config
    # configure` offers to reuse an existing one or create it on the spot.
    cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"

    # Plain typed fields, backed by the [tools.my_package] TOML section
    backend: str = Field(default="default", description="Backend to use.")
    data_path: str | None = Field(default=None, description="Path to local data files.")
```

`get_config()` is inherited from `PackageConfigBase`: call `MyPackageConfig.get_config()` to load from the active stack config. It delegates to `Resolver.resolve_package_config()`, which reads the `[tools.my_package]` section and validates every `RefTo`-marked field against it. If the section is missing, fields fall back to their defaults.

The configure workflow validates the assembled `[tools.my_package]` values with
`MyPackageConfig` before saving. This includes scalar and nested constraints,
cross-field model validators, missing references, and wrong-kind references.
Invalid candidates leave the existing file unchanged. Values accepted from CLI
strings are saved from the validated model, so their normalized Python types are
retained in TOML.

Validation applies to defaulted `RefTo` fields as well as explicitly supplied
ones. In a non-interactive setup, create those targets before configuring the
package or pass a nested value that creates the target chain. The interactive
flow can offer to create or select the missing target.

For a candidate assembled by another frontend, plan without file I/O:

```python
from oa_configurator import plan_configure

candidate = plan_configure(MyPackageConfig, current_config, proposed_values)
```

The function returns a new complete stack and never mutates `current_config`.
It can create or update nested `RefTo` targets from nested dictionaries, uses
stored values for omitted fields, and preserves `current_config.loaded_path` as
provenance. `PackageConfigValidationError.errors()` retains the original
pydantic field locations for structured presentation. Persistence is always a
separate, explicit `save_stack_config(candidate)` operation. Missing or invalid
nested values raise library configuration exceptions without printing CLI text
or raising `typer.Exit`.

---

### 3. Register the entry point

```toml
[project.entry-points."omop.config"]
my_package = "my_package.config:MyPackageConfig"
```

After installing your package, `omop-config configure my_package` will find and prompt through your fields.

---

### 4. Engine creation

```python
from oa_configurator import Resolver

config = MyPackageConfig.get_config()
engine = Resolver.from_active_config().resolve_database(config.cdm_db).create_engine()
```

`create_engine()` applies the `schema_translate_map` automatically so OMOP ORM models route to the right schemas without changes.

For the vocabulary database:

```python
from oa_configurator import Role

vocab_engine = Resolver.from_active_config().resolve_database(config.cdm_db).create_engine(role=Role.VOCAB)
```

---

### 5. Logging

At your package's CLI entry point or startup:

```python
from oa_configurator import configure_logging

# verbosity comes from the -v/-vv CLI flag count
configure_logging(verbosity=verbose, extra_namespaces=["my_package"])
```

Pass `load_stack_config()` as the first argument instead of `verbosity=` to use the `[logging]` block from the config file:

```python
configure_logging(load_stack_config(), verbosity=verbose, extra_namespaces=["my_package"])
```

---

## Testing

Tests fall into two tiers with different requirements.

### Unit tests: `StackConfig.for_session()`

Unit and mock-based tests must never touch `~/.config/omop/config.toml`. Use `StackConfig.for_session()` with `monkeypatch` to inject a fully in-memory config:

```python
from oa_configurator import StackConfig, Resolver

def test_something(monkeypatch):
    cfg = StackConfig.for_session(
        connections={"db": {"dialect": "sqlite", "database_name": ":memory:"}},
        databases={"cdm": {"kind": "cdm", "connection": "db", "schema_name": "omop"}},
        tools={"my_package": {"backend": "test_backend"}},
    )
    monkeypatch.setattr("my_package.module.load_stack_config", lambda: cfg)
    # ... test against the in-memory config
```

This covers the vast majority of tests. No file I/O, no environment-specific setup needed.

!!! success "`for_session() + monkeypatch` is NOT a fallback"
    `for_session()` + `monkeypatch` is for isolated unit tests where only config *values* matter, not config *source*. It **MUST NEVER** be used to paper over missing configuration in CI or local dev. If a code path needs `get_config()`/`load_stack_config()` to succeed at all, you
    need to provide a real configuration for the test case.

### Integration tests: a dedicated test database

For tests that exercise a real database (e.g. PostgreSQL-specific SQL, bulk loading, trigger management), use a **dedicated named database** in the user's config, never a copy of the production database under a different guise.

The convention is `test_<package>_db` (e.g. `test_cdm_db` for omop-alchemy) for readability, but what actually marks a field as a test field is `RefTo(CDMDatabaseConfig, is_test=True)` (or `RefTo(GenericDatabaseConfig, is_test=True)`, whichever kind the package's real database is). The field's own Python name carries no meaning to `oa-configurator` itself. Keeping the *value* distinct from the production database (`cdm_db`) is still a mandatory safety guard: the test suite must never accidentally connect to a production database.

In `conftest.py`, resolve the test database via the `resolve_test_database` pytest-plugin helper, which skips cleanly whether `config.toml` is entirely missing or simply doesn't have this database configured yet:

```python
@pytest.fixture(scope="session")
def pg_engine():
    from oa_configurator.pytest_plugin import resolve_test_database
    from my_package.config import MyPackageConfig

    url = resolve_test_database(MyPackageConfig, "test_cdm_db")
    engine = sa.create_engine(url, future=True)
    yield engine
    engine.dispose()
```

`resolve_test_database(cls, field_name)` always takes the field name explicitly, with no auto-discovery, so it resolves without requiring the rest of your package's config (e.g. a required `cdm_db` field) to also be configured. That's deliberate on both counts: a CI runner that only provisions a test database shouldn't need a "production" database configured just to find it, and a class with more than one `is_test=True` field (e.g. one per backend) would otherwise have no way to say which one a caller means.

#### Provisioning the test database

The test database must be provisioned for real before pytest runs. There is no fallback that papers over a missing one, by design (see the callout above).

A package field marked `is_test=True` (e.g. `test_cdm_db: Annotated[str | None, RefTo(CDMDatabaseConfig, is_test=True)] = None`) gets special handling in `omop-config configure <package>`'s **interactive** flow: it asks whether to configure a test database, and if you accept, recurses through creating both the connection and the database, marking the connection `test_only = true` automatically and refusing to reuse (or collide with) a non-test connection's host/database combination. `resolve_package_config()` (used by `get_config()` and this interactive flow alike) separately enforces, every time your config loads, that an `is_test=True` field always resolves to a `test_only=true` connection and an `is_test=False` field never does. Pointing either one at the wrong kind of connection raises `ConfigurationError` immediately, not just when a test happens to run.

Non-interactively, `--test-only` is an ordinary flag on `connections add` (accepting `true`/`false`/`yes`/`no`/`1`/`0`):

```bash
omop-config connections add test_cdm \
  --dialect postgresql+psycopg --host localhost --port 5432 \
  --user test --password test --database-name test_db --test-only true

omop-config databases add test_cdm_db --kind cdm --connection test_cdm --schema-name public

omop-config configure <package> --test-cdm-db test_cdm_db
```

The `--test-cdm-db` flag above is the field's own auto-generated flag (`test_cdm_db` -> `--test-cdm-db`); it points the field at an already-created database by name, same as any other `RefTo` field passed non-interactively. Or do it in the single `configure` call directly with `--set` (see [Docker Compose](#docker-compose) below):

```bash
omop-config configure <package> \
  --set test_cdm_db.kind=cdm \
  --set test_cdm_db.connection.dialect=postgresql+psycopg \
  --set test_cdm_db.connection.host=localhost \
  --set test_cdm_db.connection.database_name=test_db \
  --set test_cdm_db.connection.test_only=true \
  --set test_cdm_db.schema_name=public
```

=== "Local development"

    Run `omop-config configure <package>` and answer `Y` when asked to configure a test database field.

=== "CI"

    Either provision the connection and database ahead of time (as part of image build or a setup step) and pass the package's test-field flag, or do it all in one `--set`-based `configure` call, as shown above.

!!! info "Safety"
    The test database must point to a dedicated, empty database.
    If your test session drops and recreates schemas, add a runtime guard that compares the
    resolved URL of `test_cdm_db` against all other configured databases and calls
    `pytest.fail()` on any match.

---

## Multiple environments

### Adding a second database of the same kind

A package's own field just names a database by default (e.g. `cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"`). To point at a second one, for example a production CDM alongside a local development one, create the extra database under its own name and pass the field's own flag:

```bash
omop-config databases add cdm_db_prod --kind cdm --connection cdm_prod --schema-name omop
omop-config configure omop_alchemy --cdm-db cdm_db_prod
```

This creates `cdm_db_prod` without touching the existing `cdm_db`, and points `omop_alchemy` at it.

### Choosing between databases of the same kind

There is no config-level "default database" toggle. When more than one database of the same kind exists, the caller names the one it wants explicitly:

```python
from omop_alchemy.config import OmopAlchemyConfig

config = OmopAlchemyConfig.get_config()
prod_engine = OmopAlchemyConfig.get_engine("cdm_db_prod")
dev_engine = OmopAlchemyConfig.get_engine(config.cdm_db)  # whatever cdm_db currently resolves to
```

For switching between whole environments (dev vs. prod) rather than picking one database among several, use distinctly-named connections and databases per environment, and point each deployment's `omop-config configure` flags at the right ones. There is no profile/overlay mechanism; naming is the only axis.

### Cross-package database references

A package that consumes a database owned by another package (e.g. `omop-graph` using `omop-alchemy`'s CDM database) declares its own field with the same `RefTo(CDMDatabaseConfig)` type and the same default name (e.g. `cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"`). There is no typed cross-package pointer: two packages share an entry simply because both fields resolve to the same name. The shared database must be configured once, under that name, for both packages' references to resolve.

---

## Docker Compose

### How it works

`~/.config/omop/config.toml` lives on the host (or in a container's home directory) and is the single source of truth. Docker Compose is only needed to provide **database credentials** at container startup. The app itself always reads from the TOML file, never from environment variables at runtime.

The workflow:

1. A gitignored `.env` file holds secrets that Docker Compose substitutes into its YAML.
2. The container's startup command calls `omop-config connections add`/`databases add` with `--flags`, then `omop-config configure <package>` to point the package at what was just created, writing those values into `config.toml` **once at startup**.
3. After that, the app reads `config.toml` normally without any environment variables involved.

The `.env` file is a Docker Compose concern only. It is never loaded by the Python app.

### Example

`.env` (gitignored):

```bash
POSTGRES_USER=omop
POSTGRES_PASSWORD=secret
POSTGRES_DB=omop_cdm
```

`docker-compose.yml`:

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}

  app:
    build: .
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    command: >
      bash -c "
        omop-config connections add cdm
          --dialect postgresql+psycopg --host db --port 5432
          --user $$POSTGRES_USER --password $$POSTGRES_PASSWORD
          --database-name $$POSTGRES_DB &&
        omop-config databases add cdm_db --kind cdm --connection cdm --schema-name omop &&
        omop-config configure my_package --cdm-db cdm_db &&
        exec my_app_entrypoint
      "
```

!!! note "$$VAR escaping"
    Use `$$VAR` (double dollar) inside a `command:` string so Docker Compose passes the
    literal variable name to the shell rather than substituting it at YAML-parse time.

If your stack has more than one package pointing at the same database (e.g., `my_package` and `omop_alchemy`), the `connections add`/`databases add` steps only need to run once; add one `configure` call per package, chained with `&&`:

```yaml
command: >
  bash -c "
    omop-config connections add cdm
      --dialect postgresql+psycopg --host db --port 5432
      --user $$POSTGRES_USER --password $$POSTGRES_PASSWORD
      --database-name $$POSTGRES_DB &&
    omop-config databases add cdm_db --kind cdm --connection cdm --schema-name omop &&
    omop-config configure omop_alchemy --cdm-db cdm_db &&
    omop-config configure my_package --cdm-db cdm_db &&
    exec my_app_entrypoint
  "
```

Each `configure` call is scoped to its own package's flags; `connections add`/`databases add` are shared setup steps run once regardless of how many packages point at the result.

### One-shot alternative: `--set`

For a single package pointing at a database nobody else needs to share, `configure` can create the connection and database in the same call via repeated `--set field.subfield=value` flags, instead of the three separate commands above:

```yaml
command: >
  bash -c "
    omop-config configure my_package
      --set cdm_db.kind=cdm
      --set cdm_db.connection.dialect=postgresql+psycopg
      --set cdm_db.connection.host=db
      --set cdm_db.connection.port=5432
      --set cdm_db.connection.user=$$POSTGRES_USER
      --set cdm_db.connection.password=$$POSTGRES_PASSWORD
      --set cdm_db.connection.database_name=$$POSTGRES_DB
      --set cdm_db.schema_name=omop &&
    exec my_app_entrypoint
  "
```

`cdm_db` here is the name of the package's own field (`Annotated[str, RefTo(CDMDatabaseConfig)]`), and `connection` is `DatabaseConfig`'s own field (shared by both kinds) naming a `[connections.*]` entry. The dotted path can go as deep as the reference chain does. The connection this creates is named after the database (`cdm_db`, from the field's own default), or pass `--set cdm_db.connection.name=<explicit-name>` to choose one. Prefer the three-command form above when more than one package needs to point at the same database, since `--set` creates a fresh one per call.

### Security note

The `config.toml` written by the container will contain the database password in plaintext. This is acceptable for local development containers. Restrict the file permissions:

```bash
chmod 600 ~/.config/omop/config.toml
```

`omop-config` will warn at load time if the file has looser permissions.

---

## TOML snippet for your README

Add a **Configuration** section to your package README:

```markdown
## Configuration

Requires `omop-config` to be run once. See the
[`oa-configurator` quickstart](link) for initial setup.

Add the following to `~/.config/omop/config.toml`:

\`\`\`toml
[tools.my_package]
backend   = "default"
data_path = "/path/to/data"
\`\`\`

Then run:
\`\`\`bash
omop-config configure my_package
\`\`\`
```
