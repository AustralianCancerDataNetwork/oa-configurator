# Quickstart

## 1. Install

```bash
pip install oa-configurator
```

## 2. Configure your first package

After installing a package that supports oa_configurator, run its configure command:

```bash
omop-config configure <package>   # e.g. omop_alchemy, omop_emb
```

This prompts for any database connections and package-specific settings that package declares, then writes `~/.config/omop/config.toml`. Packages that own a database resource (such as a CDM database) will prompt for connection details and schema names as part of this step.

To create an empty config file without prompts (useful for scripted setups), use:

```bash
omop-config init
```

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

- Switch profiles: `omop-config use <profile>` (see [Profiles](profiles.md))
- Configure a package interactively: `omop-config configure omop_alchemy` (see [Integration](integration.md))
- All TOML fields: [Config reference](config-reference.md)
