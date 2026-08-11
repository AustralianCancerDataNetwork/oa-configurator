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

This prompts for any database connections and package-specific settings that package declares, then writes `~/.config/omop/config.toml`. Each package that depends on `oa-configurator` for functionality defines its own configuration, which is then interactively generated using the above command.
To create an empty config file without prompts (useful for scripted setups), use:

```bash
omop-config init
```

!!! warning "Safety"
    Passwords and API Keys are currently stored in plaintext. Keep the file readable only by your user:
    `chmod 600 ~/.config/omop/config.toml`.
    Secret management improvements are planned for a future release.

## 3. Configure an LLM/embedding model (optional)

Packages that call an LLM or embedding model (e.g. `omop-emb`, `omop-spires`) reference one by name via their own package-specific setting. For fields the package marks for this, `omop-config configure <package>` resolves them for you: it offers to reuse an existing `[models.*]` entry, or create one on the spot, recursing into `[providers.*]` the same way if the provider doesn't exist yet either.

To manage `[providers.*]`/`[models.*]` entries directly, outside of any specific package's configure flow, use the standalone commands:

```bash
omop-config providers add <provider-name>   # e.g. local-ollama
omop-config models add <model-name>         # e.g. nomic-embed
```

`providers add` prompts for the `omop-llm` provider key (`ollama`, `llamacpp`, `vllm`, `openai`, `anthropic`, `gemini`), base URL, and API key. `models add` prompts for which provider it's served through, the model name, `embedding_dim`/`document_prefix`/`query_prefix`, and then for each of the four capability fields — `embeddings`, `tool_use`, `structured_output`, `extended_thinking` — as a yes/no confirm. Capabilities are opt-in and default to `false`: neither any-llm nor `omop-llm` can introspect them per model, so anything you don't declare is treated as unsupported. An embedding model therefore needs `embeddings = true`, and setting `embedding_dim` without it is rejected outright.

Both commands accept flags for non-interactive use, with the capability fields as paired boolean flags:

```bash
omop-config models add nomic-embed \
    --provider local-ollama --model nomic-embed-text:v1.5 \
    --embeddings --embedding-dim 768 \
    --document-prefix "search_document: " --query-prefix "search_query: "
```

Passing *any* flag switches the command out of interactive mode: unflagged fields then fall back to their stored value (when updating an existing entry) or their default (when creating a new one) instead of prompting, and a required field with neither is an error. Run with `--help` for the full list.

List what's configured:

```bash
omop-config providers list
omop-config models list
```

See [Config reference](config-reference.md#providersname) for the full field list, and `omop-llm`'s [Asymmetric Embeddings guide](https://AustralianCancerDataNetwork.github.io/omop-llm/usage/asymmetric-embeddings/) for what `document_prefix`/`query_prefix` are for.

## 4. Verify

```bash
omop-config show
```

Prints your config as JSON. Validation errors (unknown field, missing cross-reference) appear here.

## 5. Test connectivity

```bash
omop-config verify
```

Reports OK / FAIL for each configured connection, with latency.

## 6. Export for Docker Compose

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

- Configure a package interactively: `omop-config configure omop_alchemy` (see [Integration](integration.md))
- All TOML fields: [Config reference](config-reference.md)
