# Secrets

Keeping passwords out of the config file is straightforward. Each connection may use `secret_source` instead of `password`; the two are mutually exclusive.

```toml
[connections.prod]
dialect       = "postgresql"
host          = "prod.hospital.org"
database      = "omop"
user          = "omop_prod"
secret_source = "env:PROD_DB_PASSWORD"   # or "file:prod.password"
```

---

## Source formats

### `env:VARIABLE_NAME`

Reads the named environment variable at resolution time.

```toml
secret_source = "env:PROD_DB_PASSWORD"
```

The variable must be set when `Resolver.resolve_connection()` is called. If it is not set, `SecretSourceResolutionError` is raised with the variable name.

### `file:path`

Reads a file from disk. Trailing newlines are stripped.

```toml
secret_source = "file:prod_cdm.password"
```

**Relative paths** resolve from `settings.secrets_dir` when that is configured, otherwise from `configuration_base_path`.

**Absolute paths** are used as-is.

```toml
secret_source = "file:/run/secrets/prod_db_password"
```

---

## `settings.secrets_dir`

An optional dedicated directory for file-backed secrets. Keeps password files separate from the config file itself.

```toml
[settings]
secrets_dir = "secrets"
```

With this configuration, `secret_source = "file:prod.password"` resolves to `{configuration_base_path}/secrets/prod.password`.

A typical layout:

```
~/.config/omop/
├── config.toml
└── secrets/
    ├── prod_cdm.password
    └── prod_vocab.password
```

Set `secrets/` to mode `700` to keep it readable only by the current user.

---

## Inline / programmatic usage

When constructing config with [`StackConfig.for_session()`](inline-usage.md), `secret_source` still works — the secret is resolved from the environment or filesystem at resolution time, same as for file-loaded configs.

```python
config = StackConfig.for_session(
    connections={
        "prod": ConnectionConfig(
            dialect="postgresql",
            host="prod.hospital.org",
            database="omop",
            user="omop_prod",
            secret_source="env:PROD_DB_PASSWORD",
        )
    },
    resources={"default": ResourceConfig(primary_db="prod")},
)
engine = Resolver(config).resolve_resource("default").create_engine()
```

---

## Safe URL and repr

All `safe_url` properties and `repr()` calls redact credentials. Only the `.url` property on a resolved connection contains the plaintext value, and it is never logged by the library.

```python
resolved = Resolver(config).resolve_connection("prod")
print(resolved.safe_url)   # postgresql://omop_prod:***@prod.hospital.org/omop
print(resolved.url)        # postgresql://omop_prod:actual_password@prod.hospital.org/omop
```
