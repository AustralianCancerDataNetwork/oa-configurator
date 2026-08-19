# Secrets

!!! info "Not yet implemented." 
    Secret management is planned for a future release.

In the current version, passwords are stored as plaintext in `~/.config/omop/config.toml`. Restrict file permissions to limit exposure:

```bash
chmod 600 ~/.config/omop/config.toml
```

## What counts as a secret

Two fields in `config.toml` hold credentials: `password` on a `[connections.*]` entry, and `api_key` on a `[providers.*]` entry. Both are *declared* secret in the schema, and every path that **renders your configuration** — the `omop-config` listings, an operator console, a package's `--describe` output — keys off that declaration rather than guessing from a field's name.

Packages that add their own configuration declare their own secrets the same way — see the [Secrets API](api/secrets.md) if you maintain one.

## What is redacted where

| Surface | Behaviour |
|---|---|
| Interactive prompts | Declared secret fields are entered with the input hidden |
| `omop-config <section> list` | Declared secret fields show `***` when set and `-` when not; no value is printed |
| `ResolvedConnection.safe_url` | Password replaced with `***`; the username is kept. The plaintext `.url` is used only to create the engine |
| `safe_endpoint()` on any other URL | Password in the `user:password@host` part replaced with `***`, query-string *values* replaced with `***`, keys kept, fragments replaced with `#***` |
| `repr()` / `str()` of any config object | Declared secret fields render as `***`, at any nesting depth |
| `omop-config show` | Declared secret fields render as `***`; everything else is printed in full |

Query-string values are masked without exception, including harmless ones, because working out which parameter is a credential would mean guessing. You still see which parameters are set: `?api-version=2024-02-01&api_key=sk-x` displays as `?api-version=***&api_key=***`.

A fragment is masked whole rather than value-by-value, because it has no guaranteed `key=value` structure to take apart.

Config objects are safe to log, print or interpolate. Every model masks its declared secrets in `repr` and `str`, at any depth, so all of these are safe:

```python
logger.debug("%s", config)
logger.debug("%r", config)
print(config)
raise ValueError(f"could not connect: {connection}")
```

Reading a secret attribute returns the real value, because that is the only way it can reach SQLAlchemy, the `.env` writer and the TOML writer:

```python
connection.password        # 'hunter2' - real value
```

So this leaks, and nothing will stop it:

```python
logger.debug("password=%s", connection.password)   # this is considered the responsibility of downstream consumers - if you ask to log a raw string like this, that's your explicit choice at that point
```

`RedactingFormatter` covers one case none of the above reaches: a URL written into a log record by *another* library, such as a SQLAlchemy connection error echoing the DSN it was handed. Those bytes never pass through a config model, so it scrubs them with `safe_endpoint`. It does not attempt anything else.

oa-configurator never logs a credential itself — its own resolver logs connections through `safe_url`.

## Credentials do not belong in `base_url`

A provider's `base_url` is rejected if it carries userinfo — the `user:password@` part before the host:

```toml
[providers.vendor]
provider = "openai"
base_url = "https://svc:hunter2@api.vendor.com/v1"   # rejected at load time
```

Put the credential in `api_key`, which is the field the stack knows to mask:

```toml
[providers.vendor]
provider = "openai"
base_url = "https://api.vendor.com/v1"
api_key  = "hunter2"
```

A credential passed as a query parameter (`?api_key=...`) is accepted, since rejecting it would mean guessing at parameter names — but it is stored in plaintext in `config.toml` like any other part of the URL, and only ever *displayed* masked. Prefer `api_key`.

## Backup copies

Each update to an existing configuration keeps the previous complete file as `config.toml.bak`. The backup is readable only by your user, but it contains the same plaintext passwords and API keys that were in the earlier configuration. Treat it exactly like `config.toml`: do not commit it, attach it to a support request, or place it in a shared folder.

`omop-config init --force` replaces the active configuration but does not delete `config.toml.bak`. If you are removing credentials from this machine, check and remove both files. Rotate any credential that should no longer grant access; deleting a local copy does not invalidate the credential itself.

If `config.toml` is a symbolic link, oa-configurator writes the backup beside the link's resolved target, not beside the link. Check the target directory for `config.toml.bak`, especially when the target is in a synchronized folder or version-controlled checkout.

---

## Planned: `secret_source`

A future `secret_source` field on `ConnectionConfig` (and `ProviderConfig`, for API keys) will support indirect credential lookup, keeping secrets out of the TOML file entirely:

```toml
[connections.prod]
dialect       = "postgresql+psycopg"
host          = "prod.hospital.org"
database_name = "omop_cdm"
user          = "omop_prod"
secret_source = "env:PROD_DB_PASSWORD"   # or "file:/run/secrets/prod.password"
```

Planned source formats:

| Format | Description |
|--------|-------------|
| `env:VARIABLE_NAME` | Read from environment variable at resolution time |
| `file:PATH` | Read from file (absolute path or relative to config dir) |
| Vault / cloud (under consideration) | AWS Secrets Manager, GCP Secret Manager, Azure Key Vault |
