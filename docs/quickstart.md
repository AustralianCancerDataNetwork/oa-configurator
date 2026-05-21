# Quickstart

## 1. Install

```bash
pip install oa-configurator
```

## 2. Create `~/.config/omop/config.toml`

```toml
active_profile = "local"

[connections.cdm]
dialect  = "postgresql+psycopg2"
host     = "localhost"
port     = 5432
user     = "omop"
password = "changeme"
database = "omop_cdm"

[resources.default]
primary_db = "cdm"
cdm_schema = "omop"
```

> **Note**: Passwords are stored in plaintext for now. Keep the file readable only by your user: `chmod 600 ~/.config/omop/config.toml`. Secret management improvements are planned for a future release.

## 3. Verify

```bash
omop-config show
```

Prints your config as JSON. Validation errors (unknown field, missing cross-reference) appear here.

## 4. Export for Docker Compose

```bash
omop-config export-env
```

Writes `~/.config/omop/config.env`. Docker Compose services read it via `env_file:`.

## 5. Test connectivity

```bash
omop-config verify
```

Reports OK / FAIL for each configured connection, with latency.

---

## Next steps

- Switch profiles: `omop-config use <profile>` — see [Profiles](profiles.md)
- Configure a package interactively: `omop-config configure omop_emb` — see [Integration](integration.md)
- All TOML fields: [Config reference](config-reference.md)
