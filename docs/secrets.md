# Secrets

!!! info "Not yet implemented." 
    Secret management is planned for a future release.

In the current version, passwords are stored as plaintext in `~/.config/omop/config.toml`. Restrict file permissions to limit exposure:

```bash
chmod 600 ~/.config/omop/config.toml
```

`ResolvedConnection.safe_url` redacts passwords in all display and log output. The plaintext `.url` is used only internally for engine creation.

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
