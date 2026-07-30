# Architecture

## Purpose

`oa-configurator` is a shared configuration layer for the OMOP-oriented Python stack:

- `omop-alchemy`
- `orm-loader`
- `omop-emb`
- `omop-graph`
- `omop-spires`

It replaces per-package `.env` files, inconsistent env var lookups, and duplicated engine-creation boilerplate with a single typed TOML file and a common resolver interface.

---

## Core concepts

### Database

A concrete database endpoint: dialect, host, credentials, database name. Stored in `[databases.<name>]`.

### Resource

A logical role bundle that maps OMOP CDM roles to databases and schema names:

- `database`: the CDM server database
- `vocab_database`: optional separate database for vocabulary (falls back to `database`)
- `cdm_schema`: schema where CDM clinical tables live (required)
- `vocab_schema` (optional): falls back to `cdm_schema`
- `results_schema` (optional): Achilles/Atlas results

### Provider

A concrete LLM/embedding provider connection: provider key, base URL, API key. Stored in `[providers.<name>]`. Peer of `Database` for LLM backends instead of databases.

### Model

A named, reusable, concretely-configured model: which provider it runs through, model name, embedding dimension, prefixes. Stored in `[models.<name>]`, references a `Provider` by name. Peer of `Resource` for LLM backends instead of databases. See [Resource/Model resolution](#resourcemodel-resolution) below: the two pairs resolve the same way.

### Tool

Per-package configuration in `[tools.<name>]`. The core model stores only an `extra` dict. Each consuming package defines a typed `PackageConfigBase` subclass that provides a typed view over `extra`.

### ResourceRef

A package that consumes a resource owned by another package (e.g. `omop-graph` using `omop-alchemy`'s CDM database) declares a `ResourceRef(OwningClass, OwningClass.SPEC)` in its `required_resources`. It pairs the owning class (`owning_class.tool_name`, used in "go configure that package" error messages) with the specific `ResourceSpec` (`spec`, since the owning class may declare more than one resource). Resolves to `spec.semantic_name`; the owning package's resource must be configured under that literal name.

### ModelFieldSpec

A CLI-only marker for a package's own field that names a `[models.*]` entry (e.g. `embedding_model_name: str`). `omop-config configure` resolves it interactively: reuse an existing entry, or create one, recursing into `[providers.*]` the same way. No effect at runtime; the field itself stays a plain `str`.

### Profile

A named overlay (`[profiles.<name>]`) that replaces specific connections, resources, or tools when active. Full model replacement, not a partial patch. Activate via `omop-config use <profile>` (persists to TOML) or `OA_ACTIVE_PROFILE=<profile>` (per-session).

---

## Resource/Model resolution

`Resource`/`Database` and `Model`/`Provider` are the same two-tier pattern: a mid-tier entry references one leaf-tier entry by name (highlighted below), and `Resolver` resolves the whole pair into one runtime object. Left of each divider is the Python attribute, right is where it lives in `config.toml`. Names below are generic, not this project's real ones.

<iframe src="../diagrams/resource-model-resolution.html" title="Resource/Model resolution diagram" style="width: 100%; border: 0; display: block;" loading="lazy"></iframe>
<script>
  (function () {
    var frame = document.currentScript.previousElementSibling;
    function resize() {
      try {
        frame.style.height = frame.contentWindow.document.body.scrollHeight + "px";
      } catch (e) {}
    }
    frame.addEventListener("load", function () {
      resize();
      new ResizeObserver(resize).observe(frame.contentWindow.document.body);
    });
  })();
</script>


---

## Data flow

<iframe src="../diagrams/config-data-flow.html" title="Config data flow diagram" style="width: 100%; border: 0; display: block;" loading="lazy"></iframe>
<script>
  (function () {
    var frame = document.currentScript.previousElementSibling;
    function resize() {
      try {
        frame.style.height = frame.contentWindow.document.body.scrollHeight + "px";
      } catch (e) {}
    }
    frame.addEventListener("load", function () {
      resize();
      new ResizeObserver(resize).observe(frame.contentWindow.document.body);
    });
  })();
</script>

---

## Package integration via entry points

Consuming packages subclass `PackageConfigBase` and register via a `pyproject.toml` entry point:

```toml
[project.entry-points."omop.config"]
my_package = "my_package.config:MyPackageConfig"
```

`omop-config configure my_package` discovers the class at runtime via `importlib.metadata.entry_points(group="omop.config")`, presents the typed fields for interactive configuration, and writes the result to `[tools.my_package.extra]`. `oa-configurator` itself has no knowledge of any consuming package.

---

## Schema translate map

`ResolvedResource.schema_translate_map()` returns the SQLAlchemy-compatible schema translate dict:

```python
{None: "omop", "vocab": "omop_vocab", "results": "results"}
```

OMOP ORM models (omop-alchemy) carry `schema=None` or `schema="vocab"` on their `__table_args__`. The translate map routes them to the correct schema at runtime without changing model definitions.

---

## Security

Passwords are stored in plaintext in `~/.config/omop/config.toml`. Restrict permissions:

```bash
chmod 600 ~/.config/omop/config.toml
```

`ResolvedDatabase.safe_url` and `ResolvedDatabase.url` are distinct: `safe_url` has the password replaced with `***` and is used for all logging and display. The `.url` value (with plaintext password) is used only for engine creation and never logged by the library.

`RedactingFormatter` (applied by all non-library log presets) scrubs both `key=value` patterns and `://user:password@host` URL patterns from log output.

**Future work**: `secret_source` support (`env:VAR`, `file:path`, Vault, cloud secret managers) is planned but not implemented in this version.

---

## Config path

Default: `~/.config/omop/config.toml`. Override with `OA_CONFIG_PATH=<path/to/config.toml>` (must end in `.toml`; `~` is expanded). Resolved once at module load time and stored as `CONFIG_PATH`. Use `OA_ACTIVE_PROFILE` to switch profiles within a file without changing the path.

---

## Future work

- `secret_source` on `DatabaseConfig`: `env:VARNAME`, `file:PATH`, Vault, cloud secret managers
- Async engine factory (`ResolvedDatabase.create_async_engine()`)
- Project-local overlay (`./oa-config.toml`) layered over user config
