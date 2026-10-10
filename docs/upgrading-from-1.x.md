# Upgrading from 1.x

Version 2.0 changes the CDM schema field and its default. For PostgreSQL CDM entries, rename `schema_name` to `cdm_schema`; `cdm_schema = "omop"` applies only to PostgreSQL. If it is omitted, the connection's own default schema or search path is used; the default is now `None`. SQLite has no schema support: remove `schema_name` from SQLite CDM entries and leave all schema fields unset.

If a PostgreSQL 1.x config relied on the implicit `omop` schema, set it explicitly:

```toml
[databases.cdm]
kind = "cdm"
connection = "cdm"
cdm_schema = "omop"
```

Run the standalone migration script against your config:

```sh
uv run https://raw.githubusercontent.com/AustralianCancerDataNetwork/oa-configurator/<tag>/migrations/to_v2.py
```

Replace `<tag>` with the oa-configurator release tag that provides the script. The script renames PostgreSQL `schema_name` to `cdm_schema`, writes `cdm_schema = "omop"` when a PostgreSQL 1.x entry omitted the field, and records schema-registry baselines.

If you do not run the script, first check the schemas for each existing database, then record its baseline once:

```sh
omop-config acknowledge-schema-migration --database <name> --reason "..."
```
