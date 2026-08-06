# oa-configurator

`oa-configurator` is a shared configuration layer for the OMOP-oriented Python stack: one typed `config.toml` file instead of a tangle of environment variables and package-local `.env` files.

## Core concepts

- **Connection**: a concrete database endpoint (dialect, host, credentials)
- **Database**: a named database built on a connection, one of two kinds: a plain generic database, or a CDM database with its vocab/results role bundle
- **Provider** / **Model**: the same two-tier pattern as Connection/Database, for LLM and embedding backends
- **Vector Store**: which storage backend an embedding-capable package should use, pointing at a generic database
- **Tool**: per-package settings, e.g. which database/model/vector store a package uses

A consuming package subclasses `PackageConfigBase`, declares its fields with `RefTo(...)` to name entries in the sections above, and registers via a `pyproject.toml` entry point. `omop-config configure <package>` then discovers it and interactively resolves or creates whatever it needs.

## Install

```bash
pip install oa-configurator
```

## Example

```python
from oa_configurator import load_stack_config, Resolver

config = load_stack_config()          # reads OA_CONFIG_PATH, default ~/.config/omop/config.toml
resolver = Resolver(config)

database = resolver.resolve_database("cdm_db")
engine = database.create_engine()     # SQLAlchemy Engine, schema_translate_map applied
```

Or without a file, for tests and scripts:

```python
from oa_configurator import StackConfig, ConnectionConfig, CDMDatabaseConfig, Resolver

config = StackConfig.for_session(
    connections={"local": ConnectionConfig(dialect="postgresql+psycopg", host="localhost",
                                            database_name="omop", password="omop")},
    databases={"cdm_db": CDMDatabaseConfig(connection="local", schema_name="omop")},
)
engine = Resolver(config).resolve_database("cdm_db").create_engine()
```

## Security

Passwords and API keys are currently stored in plaintext. Restrict file permissions:

```bash
chmod 600 ~/.config/omop/config.toml
```

`secret_source` (env-var- and file-backed credential lookup, keeping secrets out of the TOML file entirely) is planned but not yet implemented; see [Secrets](https://AustralianCancerDataNetwork.github.io/oa-configurator/secrets/).

## Docs

Full documentation, including the config file reference, CLI usage, and package-integration guide: https://AustralianCancerDataNetwork.github.io/oa-configurator/
