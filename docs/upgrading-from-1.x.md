# Upgrading from 1.x

## What changed

- On CDM entries, `schema_name` is now `cdm_schema`.
- The `cdm_schema` default is now `None`, which uses the connection's default schema or search path.
- SQLite rejects explicit schemas. Leave both `schema_name` and `cdm_schema` unset.
- The schema registry needs a baseline for each existing populated database before it can track schema changes.
- omop-emb registries are now scoped per database entry and live in their own schema. Existing registries raise `LegacyRegistryError` until they are migrated.

## Migration script

The script upgrades the config, the schema-registry baselines, and the omop-emb embedding registries without touching your data. It is a dry run by default and prints every change:

```sh
uv run https://raw.githubusercontent.com/AustralianCancerDataNetwork/oa-configurator/main/migrations/to_v2.py
```

Review the output, then make the changes:

```sh
uv run https://raw.githubusercontent.com/AustralianCancerDataNetwork/oa-configurator/main/migrations/to_v2.py --apply
```

It reads the config from `OA_CONFIG_PATH`, or `~/.config/omop/config.toml`. Use `--config PATH` for another file and `--backup-dir DIR` to choose where backups go.

### What it does

1. **Config.** Applies the `schema_name` changes from the manual steps below and validates the result against the 2.0 config before writing. Comments are kept. `--rename OLD=NEW` renames a `[databases.*]` entry (repeatable) and updates vector stores that reference it. `[tools.*]` sections naming the old entry are reported, not changed.
2. **Schema-registry baselines.** Records a baseline for every populated database and role, as `omop-config acknowledge-schema-migration` does. A database whose vocabulary sits on its own connection gets baselines on both. Empty schemas are skipped.
3. **omop-emb registries.** Moves each legacy `model_registry` into the 2.0 registry, keeping every `storage_identifier` and normalising provider names such as `OLLAMA` to `ollama`. Embedding tables are not touched. The old table is renamed to `model_registry_legacy_v1`, never dropped, so it can be rolled back.
4. **Verify.** Opens every CDM engine and vector-store registry read-only and prints OK or FAIL for each.

The run stops before changing anything if a legacy registry sits in a schema its vector store does not own, if the same model appears in more than one registry of one database, or if a registry row has no provider.

### Safety

- `--apply` copies the config and every SQLite file it may change into the backup directory first. For PostgreSQL there is no dump: the renamed `model_registry_legacy_v1` table is the rollback.
- Running it again changes nothing. An interrupted run resumes where it stopped.
- Only PostgreSQL and SQLite are supported, as in 1.x.

## Manual upgrade

1. Back up your `config.toml`.
2. Update each `[databases.*]` entry with `kind = "cdm"`:
   - **PostgreSQL:** rename `schema_name` to `cdm_schema`. If the 1.x entry had no schema field because it used the implicit `"omop"` schema, add `cdm_schema = "omop"`.
   - **SQLite:** delete `schema_name` and leave `cdm_schema` unset.
3. Confirm that the config loads by running the existing read-only command `omop-config show`.
4. For each existing populated database, inspect its schemas and confirm the configured schemas are correct. Then establish its registry baseline once:

   ```sh
   omop-config acknowledge-schema-migration --database <name> --reason "upgrade to 2.0"
   ```

omop-emb embedding stores have no manual path. Use the migration script above.
