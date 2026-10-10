# Upgrading from 1.x

## What changed

- On CDM entries, `schema_name` is now `cdm_schema`.
- The `cdm_schema` default is now `None`, which uses the connection's default schema or search path.
- SQLite rejects explicit schemas. Leave both `schema_name` and `cdm_schema` unset.
- The schema registry needs a baseline for each existing populated database before it can track schema changes.

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

## omop-emb embedding stores

Existing embedding stores do not have a manual upgrade path yet. With the new omop-emb release, they raise `LegacyRegistryError` until the migration script is available. If you need to keep using an existing embedding store, stay on your current omop-emb release and its dependencies until then.

## Migration script (coming in a later release)

The script is currently a placeholder and does not change files. When the real script ships, run it with:

```sh
uv run https://raw.githubusercontent.com/AustralianCancerDataNetwork/oa-configurator/main/migrations/to_v2.py
```

Track its availability [here](https://github.com/AustralianCancerDataNetwork/oa-configurator/issues/40).
