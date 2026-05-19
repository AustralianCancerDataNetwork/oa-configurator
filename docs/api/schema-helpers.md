# Schema Helpers

Helpers for translating resolved resource schema fields into SQLAlchemy connection options.

`ResolvedResource.create_engine()` and `ResolvedResource.schema_translate_map()` call these internally. Use the top-level function directly when you need the map without creating an engine.

::: oa_configurator.schema_helpers
