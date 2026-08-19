# Secrets API

Everything the stack knows about which values are secret comes from the `Sensitive()` marker on a field declaration. 

## Declaring a secret

Use `Secret` for the ordinary case — an optional string that holds a credential:

```python
from oa_configurator import Secret
from pydantic import BaseModel, Field

class MyToolConfig(BaseModel):
    endpoint: str
    api_key: Secret = Field(default=None, description="Token for the upstream API.")
```

::: oa_configurator.refs.Secret

::: oa_configurator.refs.Sensitive

Spell out `Annotated[..., Sensitive()]` directly where the field is not an optional string — a required secret, or one that is not a `str`.

## Reading the declaration

::: oa_configurator.refs.is_sensitive

`is_sensitive()` takes a `FieldInfo`, so it works off any model:

```python
from oa_configurator import ConnectionConfig, is_sensitive

for name, info in ConnectionConfig.model_fields.items():
    if is_sensitive(info):
        print(name)   # -> password
```

## Scrubbing a URL for display

::: oa_configurator.refs.safe_endpoint

::: oa_configurator.refs.MASK

## Making your own models safe to render

`PackageConfigBase` already inherits `SecretSafeModel`, so a consuming package's config section is masked in `repr`/`str` without doing anything. Subclass it directly only for a *nested* model of your own that is not a `PackageConfigBase`.

::: oa_configurator.refs.SecretSafeModel

Use `safe_endpoint()` for any URL-shaped value, `ProviderConfig.base_url` above all. For a database connection, [`ConnectionConfig.safe_url()`](resources.md) is the SQLAlchemy-specific equivalent.

```python
from oa_configurator import safe_endpoint

safe_endpoint("https://svc:hunter2@api.example.org/v1?api-version=2024-02-01&api_key=sk-x")
# 'https://svc:***@api.example.org/v1?api-version=***&api_key=***'

safe_endpoint("https://api.example.org/v1#access_token=sk-x")
# 'https://api.example.org/v1#***'
```

## Proving a package does not leak

::: oa_configurator.conformance.assert_no_sensitive_values_leak

::: oa_configurator.conformance.SensitiveValueLeak

Point it at a config object and whatever your package renders from it. Give the secrets a distinctive canary value first — the check is a substring search, so a password of `"x"` matches almost any output:

```python
from oa_configurator import ConnectionConfig, StackConfig, assert_no_sensitive_values_leak

CANARY = "canary-8f21c0-do-not-render"

def test_snapshot_redacts_secrets():
    stack = StackConfig.for_session(
        connections={"cdm": ConnectionConfig(dialect="sqlite", database_name=":memory:", password=CANARY)},
    )
    assert_no_sensitive_values_leak(stack, my_package.snapshot(stack))
```

Run it over every surface that renders configuration — a TUI snapshot, a `--describe` payload, an MCP tool response, a CLI listing. It walks nested models and models held in lists and dicts, so a whole `StackConfig` can be passed in one call.
