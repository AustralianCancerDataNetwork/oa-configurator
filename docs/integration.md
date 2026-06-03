# Integration

!!! info
    This guide showcases how to make your package support `omop-config configure <package>` and use `oa-configurator` for engine creation and logging.

---

## Overview

Each package that integrates with `oa-configurator`:

1. Subclasses `PackageConfigBase` with its typed config fields
2. Registers the class via an entry point in `pyproject.toml`
3. Calls `PackageConfigBase.from_stack(load_stack_config())` to read its config
4. Uses `Resolver(load_stack_config()).resolve_resource("default").create_engine()` for SQLAlchemy
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
from oa_configurator import PackageConfigBase, Resolver, load_stack_config


class MyPackageConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "my_package"   # maps to [tools.my_package] in TOML

    # Declare typed fields; they're backed by ToolConfig.extra in the TOML
    backend: str = Field(default="default", description="Backend to use.")
    data_path: str | None = Field(default=None, description="Path to local data files.")


def get_resolver() -> Resolver:
    return Resolver(load_stack_config())


def get_config() -> MyPackageConfig:
    return MyPackageConfig.from_stack(load_stack_config())
```

`from_stack()` reads the `[tools.my_package.extra]` section and validates it against your typed fields. If the section is missing, fields fall back to their defaults.

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
from my_package.config import get_resolver

engine = get_resolver().resolve_resource("default").create_engine()
```

`create_engine()` applies the `schema_translate_map` automatically so OMOP ORM models route to the right schemas without changes.

For the vocabulary database:

```python
vocab_engine = get_resolver().resolve_resource("default").create_engine(role="vocab")
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

### Unit tests — `StackConfig.for_session()`

Unit and mock-based tests must never touch `~/.config/omop/config.toml`. Use
`StackConfig.for_session()` with `monkeypatch` to inject a fully in-memory config:

```python
from oa_configurator import StackConfig, Resolver

def test_something(monkeypatch):
    cfg = StackConfig.for_session(
        connections={"db": {"dialect": "sqlite", "database": ":memory:"}},
        resources={"default": {"primary_db": "db", "cdm_schema": "omop"}},
        tools={"my_package": {"extra": {"backend": "test_backend"}}},
    )
    monkeypatch.setattr("my_package.module.load_stack_config", lambda: cfg)
    # ... test against the in-memory config
```

This covers the vast majority of tests. No file I/O, no environment-specific setup needed.

### Integration tests — dedicated test resource

For tests that exercise a real database (e.g. PostgreSQL-specific SQL, bulk loading, trigger
management), use a **dedicated named resource** in the user's config — never a profile override
of the production resource.

The canonical resource name is `test_<package>_db` (e.g. `test_cdm_db` for omop-alchemy).
Keeping the name distinct from the production resource (`cdm_db`) is a mandatory safety guard:
the test suite must never accidentally connect to a production database.

In `conftest.py`, resolve the test resource explicitly:

```python
_TEST_RESOURCE = "test_cdm_db"

@pytest.fixture(scope="session")
def pg_engine():
    # Path 1: CI sets ENGINE_CDM env var — no file I/O needed
    url = os.getenv("ENGINE_CDM")
    if url:
        return sa.create_engine(url, future=True)

    # Path 2: local dev — read dedicated test resource from config
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
[connections.pg_test]
dialect  = "postgresql+psycopg"
host     = "localhost"
port     = 5432
user     = "test"
password = "test"
database = "test_db"

[resources.test_cdm_db]
primary_db = "pg_test"
cdm_schema = "public"
```

> **Safety**: the test resource must point to a dedicated, empty database.
> If your test session drops and recreates schemas, add a runtime guard that compares the
> resolved URL of `test_cdm_db` against all other configured resources and calls
> `pytest.fail()` on any match.

---

---

## Docker Compose

### How it works

`~/.config/omop/config.toml` lives on the host (or in a container's home directory) and is
the single source of truth.  Docker Compose is only needed to provide **database credentials**
at container startup — the app itself always reads from the TOML file, never from environment
variables at runtime.

The workflow:

1. A gitignored `.env` file holds secrets that Docker Compose substitutes into its YAML.
2. The container's startup command calls `omop-config configure <package>` with `--flags`,
   writing those values into `config.toml` **once at startup**.
3. After that, the app reads `config.toml` normally — no environment variables involved.

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
          --conn-name cdm --dialect postgresql+psycopg
          --host db --port 5432
          --user $$POSTGRES_USER --password $$POSTGRES_PASSWORD
          --database $$POSTGRES_DB --cdm-schema omop &&
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
      --conn-name cdm --dialect postgresql+psycopg
      --host db --port 5432 --user $$POSTGRES_USER --password $$POSTGRES_PASSWORD
      --database $$POSTGRES_DB --cdm-schema omop &&
    omop-config configure my_package
      --conn-name cdm --dialect postgresql+psycopg
      --host db --port 5432 --user $$POSTGRES_USER --password $$POSTGRES_PASSWORD
      --database $$POSTGRES_DB --cdm-schema omop &&
    exec my_app_entrypoint
  "
```

Each call is scoped to its own package — the `--host` flag for `omop_alchemy configure`
configures the CDM database; the `--host` flag for `my_package configure` configures that
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
