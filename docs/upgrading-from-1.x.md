# Upgrading from 1.x

Version 2.0 changes the CDM schema field and its default. In a `kind = "cdm"` database entry, rename `schema_name` to `cdm_schema`. If `cdm_schema` is omitted, the connection's own default schema or search path is used; the default is now `None`.

If your 1.x config relied on the implicit `omop` schema, set it explicitly:

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

Replace `<tag>` with the oa-configurator release tag that provides the script. The script renames `schema_name` to `cdm_schema` and writes `cdm_schema = "omop"` when a 1.x entry omitted the field.
