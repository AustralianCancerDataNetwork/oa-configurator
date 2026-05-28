# Profiles

Profiles let you maintain one config file for multiple environments (local dev, staging, prod, test) without duplicating connection blocks.

---

## How profiles work

A profile defines connections, resources, and/or tools that **replace** the base entries with the same name when that profile is active. Base entries not mentioned in the profile are used unchanged.

```toml
# Base config
[connections.cdm]
dialect  = "postgresql+psycopg"
host     = "prod.example.com"
port     = 5432
user     = "omop"
password = "prod_secret"
database = "omop_cdm"

[resources.default]
primary_db = "cdm"
cdm_schema = "omop"

# Test profile — only what changes
[profiles.test.connections.cdm]
dialect  = "postgresql+psycopg"
host     = "localhost"
port     = 5433
user     = "test_user"
password = "test_pass"
database = "omop_test"

[profiles.test.resources.default]
primary_db = "cdm"
cdm_schema = "test_omop"
```

When the `test` profile is active, `resolver.resolve_connection("cdm")` returns the test host/database.

---

## Switching profiles

**Persistent** — writes `active_profile` to the TOML file and re-exports `config.env`:

```bash
omop-config use test
omop-config use local
```

**Per-session** — env var override, does not modify the file:

```bash
OA_ACTIVE_PROFILE=test omop-config doctor
OA_ACTIVE_PROFILE=prod python my_script.py
```

---

## Profiles in tests

Package tests should use `StackConfig.for_session()` (no file I/O). For integration tests that need a real database:

```python
# conftest.py
import os
import pytest

@pytest.fixture(autouse=True, scope="session")
def use_test_profile():
    os.environ["OA_ACTIVE_PROFILE"] = "test"
    yield
    os.environ.pop("OA_ACTIVE_PROFILE", None)
```

This requires a `[profiles.test]` section in `~/.config/omop/config.toml` pointing at the test database. Document this in your package's README.

---

## Profile with additional connections

Profiles can introduce new connections not present in the base config:

```toml
[profiles.local_only.connections.local_vocab]
dialect  = "sqlite"
database = "/data/vocab.db"

[profiles.local_only.resources.default]
primary_db = "cdm"          # uses base cdm connection
vocab_db   = "local_vocab"  # new connection defined in this profile
cdm_schema = "omop"
```

Cross-references in profile resources are validated against both base and profile connections at load time.
