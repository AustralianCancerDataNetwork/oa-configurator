# Quickstart

## 1. Install

```bash
pip install oa-configurator
```

## 2. Create `~/.config/omop/config.toml`

Run the interactive setup wizard:

```bash
omop-config init
```

This prompts for a connection (host, dialect, credentials) and a resource (schema names), then writes `~/.config/omop/config.toml`.

> **Note**: Passwords are stored in plaintext. Keep the file readable only by your user:
> `chmod 600 ~/.config/omop/config.toml`.
> Secret management improvements are planned for a future release.

## 3. Verify

```bash
omop-config show
```

Prints your config as JSON. Validation errors (unknown field, missing cross-reference) appear here.

## 4. Test connectivity

```bash
omop-config verify
```

Reports OK / FAIL for each configured connection, with latency.

## 5. Export for Docker Compose

```bash
omop-config export-env
```

Writes `~/.config/omop/config.env`. Docker Compose services read it via `env_file:`.

---

## Verbosity

All `omop-config` commands accept `-v` / `-vv` to increase log output:

```bash
omop-config -v verify     # INFO level
omop-config -vv show      # DEBUG level
```

---

## Next steps

- Switch profiles: `omop-config use <profile>` — see [Profiles](profiles.md)
- Configure a package interactively: `omop-config configure omop_alchemy` — see [Integration](integration.md)
- All TOML fields: [Config reference](config-reference.md)
