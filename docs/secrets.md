# Secrets

!!! info "Not yet implemented." 
    Secret management is planned for a future release.

In the current version, passwords are stored as plaintext in `~/.config/omop/config.toml`. Restrict file permissions to limit exposure:

```bash
chmod 600 ~/.config/omop/config.toml
```

`ResolvedConnection.safe_url` redacts passwords in all display and log output. The plaintext `.url` is used only internally for engine creation.

## Backup copies

When an existing configuration is replaced, oa-configurator retains its
previous complete contents in `config.toml.bak`. The backup is created with
mode `0600`, but it contains the same plaintext secrets as the earlier
configuration and should be protected, copied, and deleted with the same care
as `config.toml`.

The backup remains after a successful save so it is available for recovery.
Consequently, `omop-config init --force` replaces the active configuration but
does not erase the previous secrets from `config.toml.bak`. When intentionally
removing local credentials, remove both files through an appropriate secure
deletion or secret-rotation procedure.

Configuration paths are expanded and resolved before writing. If
`config.toml` is a symbolic link, the backup is placed beside the resolved
target rather than beside the link. Check that location before using a target
inside a synchronized folder or version-controlled checkout.

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
