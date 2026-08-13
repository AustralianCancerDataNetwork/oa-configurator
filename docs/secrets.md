# Secrets

!!! info "Not yet implemented." 
    Secret management is planned for a future release.

In the current version, passwords are stored as plaintext in `~/.config/omop/config.toml`. Restrict file permissions to limit exposure:

```bash
chmod 600 ~/.config/omop/config.toml
```

`ResolvedConnection.safe_url` redacts passwords in all display and log output. The plaintext `.url` is used only internally for engine creation.

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
