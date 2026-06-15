# oa-configurator

`oa-configurator` is a shared configuration layer across the OMOP-oriented public stack.

## Goals

- one obvious human-managed config file
- typed config models for code usage
- named connections and named logical resources
- support for single-DB and split-DB deployments
- support for local file-based artifacts like DuckDB and FAISS
- clean migration path from today’s env-var-heavy tools
- stable relative path semantics via one configuration base directory
- opt-in file or env backed secret resolution for connection passwords

## App Shape

This folder currently includes:

- a standalone package scaffold
- typed models for config structure
- a TOML loader
- a simple resolver for logical resources and tool defaults
- an example `config.toml`
- an architecture note describing the intended evolution

## Proposed Default Config Location

```text
~/.config/omop/config.toml
```

This prototype does not force that location, but it uses it as the default when no explicit path is provided.

When a profile defines `resource_overrides` or `tool_overrides`, those patches
are applied by the resolver at access time rather than mutating the raw loaded
config object.

Connection passwords may be provided inline with `password` or indirectly with
`secret_source`. Supported secret source formats are:

- `env:VARIABLE_NAME`
- `file:relative/or/absolute/path`

When `settings.secrets_dir` is configured, relative `file:` sources resolve
from that directory. Otherwise they resolve from `configuration_base_path`.

## Example

```python
from oa_configurator import load_stack_config, Resolver

config = load_stack_config()
resolver = Resolver(config)

resource = resolver.resolve_resource("default")
tool = resolver.resolve_tool("omop_emb")

print(resource.database.database_name)
print(resource.database.safe_url)
print(resource.vocab_db_is_primary_fallback)
print(resource.schema_translate_map())
print(tool.backend)
print(resolver.configuration_base_path)
print(config.secrets_dir)
print(resolver.active_profile_name())
```

## Notes

- This is still a sketch library, not a production-ready replacement.
- The resolver API is intentionally small for now.
- Compatibility shims for legacy env var consumers are described in `docs/architecture.md` but not implemented yet.
