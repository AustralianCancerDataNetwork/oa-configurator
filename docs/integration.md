# Integration

!!! info
    This guide showcases how to make your package support `omop-config configure <package>` and use `oa-configurator` for engine creation and logging.

---

## Overview

Each package that integrates with `oa-configurator`:

1. Subclasses `PackageConfigBase` with its typed config fields
2. Registers the class via an entry point in `pyproject.toml`
3. Calls `MyPackageConfig.get_config()` to read its config
4. Uses `Resolver.from_active_config().resolve_resource("default").create_engine()` for SQLAlchemy
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
from typing import ClassVar
from pydantic import Field
from oa_configurator import PackageConfigBase


class MyPackageConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "my_package"   # maps to [tools.my_package] in TOML

    # Declare typed fields; they're backed by ToolConfig.extra in the TOML
    backend: str = Field(default="default", description="Backend to use.")
    data_path: str | None = Field(default=None, description="Path to local data files.")
```

`get_config()` is inherited from `PackageConfigBase` — call `MyPackageConfig.get_config()` to load from the active stack config. `from_stack()` reads the `[tools.my_package.extra]` section and validates it against your typed fields. If the section is missing, fields fall back to their defaults.

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

engine = Resolver.from_active_config().resolve_resource("default").create_engine()
```

`create_engine()` applies the `schema_translate_map` automatically so OMOP ORM models route to the right schemas without changes.

For the vocabulary database:

```python
vocab_engine = Resolver.from_active_config().resolve_resource("default").create_engine(role="vocab")
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

Unit and mock-based tests must never touch `~/.config/omop/config.toml`. Use
`StackConfig.for_session()` with `monkeypatch` to inject a fully in-memory config:

```python
from oa_configurator import StackConfig, Resolver

def test_something(monkeypatch):
    cfg = StackConfig.for_session(
        databases={"db": {"dialect": "sqlite", "database_name": ":memory:"}},
        resources={"default": {"database": "db", "cdm_schema": "omop"}},
        tools={"my_package": {"extra": {"backend": "test_backend"}}},
    )
    monkeypatch.setattr("my_package.module.load_stack_config", lambda: cfg)
    # ... test against the in-memory config
```

This covers the vast majority of tests. No file I/O, no environment-specific setup needed.

### Integration tests: dedicated test resource

For tests that exercise a real database (e.g. PostgreSQL-specific SQL, bulk loading, trigger
management), use a **dedicated named resource** in the user's config and never a profile override
of the production resource.

The canonical resource name is `test_<package>_db` (e.g. `test_cdm_db` for omop-alchemy).
Keeping the name distinct from the production resource (`cdm_db`) is a mandatory safety guard:
the test suite must never accidentally connect to a production database.

In `conftest.py`, resolve the test resource explicitly:

```python
_TEST_RESOURCE = "test_cdm_db"

@pytest.fixture(scope="session")
def pg_engine():
    # CI: sets ENGINE_CDM env var no file I/O needed
    url = os.getenv("ENGINE_CDM")
    if url:
        return sa.create_engine(url, future=True)

    # Local Dev:  read dedicated test resource from config
    from oa_configurator import Resolver, load_stack_config
    from oa_configurator.package_base import ConfigurationError
    try:
        stack = load_stack_config()
        resolved = Resolver(stack).resolve_resource(_TEST_RESOURCE)
        return resolved.create_engine()
    except (FileNotFoundError, KeyError, ConfigurationError):
        pytest.skip("No PostgreSQL test database configured.")
```

**Why not `OA_ACTIVE_PROFILE=test`?** Setting `OA_ACTIVE_PROFILE` globally in `conftest.py`
affects every test that calls `load_stack_config()`, including unit tests that monkeypatch it.
A dedicated resource name scopes the real-DB resolution to only the fixture that needs it, and
leaves `cdm_db` unambiguously pointing at production data throughout the test session.

Add the test resource to `~/.config/omop/config.toml`:

```toml
[databases.pg_test]
dialect       = "postgresql+psycopg"
host          = "localhost"
port          = 5432
user          = "test"
password      = "test"
database_name = "test_db"

[resources.test_cdm_db]
database   = "pg_test"
cdm_schema = "public"
```

> **Safety**: the test resource must point to a dedicated, empty database.
> If your test session drops and recreates schemas, add a runtime guard that compares the
> resolved URL of `test_cdm_db` against all other configured resources and calls
> `pytest.fail()` on any match.

---

## Multiple environments

### Adding a second resource of the same type

The `omop-config configure` command creates one resource per semantic name by default
(e.g. `cdm_db` for omop-alchemy). To add a second — for example a production CDM
alongside a local development one — use `--resource-name`:

```bash
omop-config configure omop_alchemy --resource-name cdm_db_prod
```

This creates `cdm_db_prod` without touching the existing `cdm_db`. Because two
resources now exist for the same package, configure automatically prompts you to
choose the default at the end of the same run — no second invocation needed.

To change the default later, edit `config.toml` directly:

```toml
[tools.omop_alchemy]
default_resource = "cdm_db_prod"
```

### When does default_resource matter?

`default_resource` is only consulted when you have multiple resources of the same type
configured. With a single CDM resource and a single EMB resource, it is set
automatically by configure and you never need to think about it.

### resource_aliases — a naming compatibility tool

`resource_aliases` is NOT for switching environments. It solves a different problem:
if your resource is named differently from what packages expect (e.g. `my_hospital_cdm`
instead of `cdm_db`), aliases let packages find it without renaming your resource:

```toml
[resource_aliases]
cdm_db = "my_hospital_cdm"   # packages asking for cdm_db get my_hospital_cdm
```

For multi-environment use, create multiple named resources and use `default_resource`
instead.

---

## Docker Compose

### How it works

`~/.config/omop/config.toml` lives on the host (or in a container's home directory) and is
the single source of truth.  Docker Compose is only needed to provide **database credentials**
at container startup. The app itself always reads from the TOML file, never from environment
variables at runtime.

The workflow:

1. A gitignored `.env` file holds secrets that Docker Compose substitutes into its YAML.
2. The container's startup command calls `omop-config configure <package>` with `--flags`,
   writing those values into `config.toml` **once at startup**.
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
        omop-config configure my_package
          --database cdm --dialect postgresql+psycopg
          --host db --port 5432
          --user $$POSTGRES_USER --password $$POSTGRES_PASSWORD
          --database-name $$POSTGRES_DB --cdm-schema omop &&
        exec my_app_entrypoint
      "
```

!!! note "$$VAR escaping"
    Use `$$VAR` (double dollar) inside a `command:` string so Docker Compose passes the
    literal variable name to the shell rather than substituting it at YAML-parse time.

If your stack has more than one package (e.g., `my_package` and `omop_alchemy`), add a
separate `omop-config configure` call for each, chained with `&&`:

```yaml
command: >
  bash -c "
    omop-config configure omop_alchemy
      --database cdm --dialect postgresql+psycopg
      --host db --port 5432
      --user $$POSTGRES_USER --password $$POSTGRES_PASSWORD
      --database-name $$POSTGRES_DB --cdm-schema omop &&
    omop-config configure my_package
      --database cdm --dialect postgresql+psycopg
      --host db --port 5432
      --user $$POSTGRES_USER --password $$POSTGRES_PASSWORD
      --database-name $$POSTGRES_DB --cdm-schema omop &&
    exec my_app_entrypoint
  "
```

Each call is scoped to its own package. The `--host` value for `omop_alchemy`
configures the CDM database; the `--host` value for `my_package` configures that
package's database.  No prefix is needed because the package name is the namespace.

### Security note

The `config.toml` written by the container will contain the database password in plaintext.
This is acceptable for local development containers.  Restrict the file permissions:

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
default_resource = "default"

[tools.my_package.extra]
backend   = "default"
data_path = "/path/to/data"
\`\`\`

Then run:
\`\`\`bash
omop-config configure my_package
\`\`\`
```
