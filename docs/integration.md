# Integration

How to make your package support `omop-config configure <package>` and use OA_Configurator for engine creation and logging.

---

## Overview

Each package that integrates with OA_Configurator:

1. Subclasses `PackageConfigBase` with its typed config fields
2. Registers the class via an entry point in `pyproject.toml`
3. Calls `PackageConfigBase.from_stack(load_stack_config())` to read its config
4. Uses `Resolver(load_stack_config()).resolve_resource("default").create_engine()` for SQLAlchemy
5. Calls `configure_logging(verbosity=verbose, extra_namespaces=["<package>"])` at startup

---

## Step 1 — Add the dependency

In your `pyproject.toml`:

```toml
[project.dependencies]
"oa-configurator>=0.2.0"
```

---

## Step 2 — Define your config class

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

## Step 3 — Register the entry point

```toml
[project.entry-points."omop.config"]
my_package = "my_package.config:MyPackageConfig"
```

After installing your package, `omop-config configure my_package` will find and prompt through your fields.

---

## Step 4 — Engine creation

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

## Step 5 — Logging

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

Package tests should never touch `~/.config/omop/config.toml`. Use `StackConfig.for_session()`:

```python
from oa_configurator import StackConfig, Resolver

def test_something():
    cfg = StackConfig.for_session(
        connections={"db": {"dialect": "sqlite", "database": ":memory:"}},
        resources={"default": {"primary_db": "db", "cdm_schema": "omop"}},
        tools={"my_package": {"extra": {"backend": "test_backend"}}},
    )
    resolver = Resolver(cfg)
    # ... test against resolver
```

For tests that need a real database, set `OA_ACTIVE_PROFILE=test` in `conftest.py` (pointing to a `[profiles.test]` section in the user's config) and call `load_stack_config()` normally.

---

## TOML snippet for your README

Add a **Configuration** section to your package README:

```markdown
## Configuration

Requires `omop-config` to be run once. See the
[OA_Configurator quickstart](link) for initial setup.

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
