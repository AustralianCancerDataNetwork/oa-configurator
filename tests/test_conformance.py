"""Tests for `assert_no_sensitive_values_leak`.

The helper is the enforcement half of the "declare, never guess" rule: since
nothing at runtime infers sensitivity from a field name, a package proves its
rendering path honours the markers by running its own output through here.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from oa_configurator import (
    CDMDatabaseConfig,
    ConnectionConfig,
    ProviderConfig,
    Secret,
    SensitiveValueLeak,
    StackConfig,
    assert_no_sensitive_values_leak,
    is_sensitive,
    safe_endpoint,
)

CANARY = "canary-8f21c0-do-not-render"


class _Nested(BaseModel):
    token: Secret = None
    label: str | None = None


class _Outer(BaseModel):
    api_key: Secret = None
    name: str | None = None
    child: _Nested | None = None
    children: list[_Nested] = []
    by_name: dict[str, _Nested] = {}


def _stack_with_secrets() -> StackConfig:
    """A config whose every declared secret carries a distinct canary."""
    return StackConfig.for_session(
        connections={
            "cdm": ConnectionConfig(
                dialect="postgresql+psycopg",
                host="db.hospital.org",
                port=5432,
                user="omop",
                password=f"{CANARY}-password",
                database_name="omop_cdm",
            )
        },
        databases={"default": CDMDatabaseConfig(connection="cdm", schema_name="omop")},
        providers={
            "azure": ProviderConfig(
                provider="openai",
                base_url="https://x.openai.azure.com/v1?api-version=2024-02-01",
                api_key=f"{CANARY}-api-key",
            )
        },
    )


class TestFlatModel:
    def test_clean_render_passes(self):
        assert_no_sensitive_values_leak(
            _Outer(api_key=CANARY, name="prod"), "api_key=***, name=prod"
        )

    def test_leaked_value_raises(self):
        with pytest.raises(SensitiveValueLeak):
            assert_no_sensitive_values_leak(
                _Outer(api_key=CANARY), f"api_key={CANARY}"
            )

    def test_error_names_the_field_path(self):
        with pytest.raises(SensitiveValueLeak, match=r"_Outer\.api_key"):
            assert_no_sensitive_values_leak(_Outer(api_key=CANARY), CANARY)

    def test_error_does_not_repeat_the_secret(self):
        """Assertion messages land in CI logs, so the leak must not be echoed."""
        with pytest.raises(SensitiveValueLeak) as excinfo:
            assert_no_sensitive_values_leak(_Outer(api_key=CANARY), CANARY)
        assert CANARY not in str(excinfo.value)

    def test_unset_secret_is_not_matched(self):
        """`None` and `""` are not values, and `""` is in every string."""
        assert_no_sensitive_values_leak(_Outer(api_key=None), "anything")
        assert_no_sensitive_values_leak(_Outer(api_key=""), "anything")

    def test_non_secret_field_may_appear(self):
        assert_no_sensitive_values_leak(_Outer(name=CANARY), f"name={CANARY}")


class TestNesting:
    def test_nested_model(self):
        instance = _Outer(child=_Nested(token=CANARY))
        assert_no_sensitive_values_leak(instance, "child.token=***")
        with pytest.raises(SensitiveValueLeak, match=r"_Outer\.child\.token"):
            assert_no_sensitive_values_leak(instance, f"child.token={CANARY}")

    def test_model_in_a_list(self):
        instance = _Outer(children=[_Nested(label="a"), _Nested(token=CANARY)])
        with pytest.raises(SensitiveValueLeak, match=r"children\[1\]\.token"):
            assert_no_sensitive_values_leak(instance, CANARY)

    def test_model_in_a_dict(self):
        instance = _Outer(by_name={"azure": _Nested(token=CANARY)})
        with pytest.raises(SensitiveValueLeak, match=r"by_name\['azure'\]\.token"):
            assert_no_sensitive_values_leak(instance, CANARY)

    def test_shared_submodel_is_visited_once(self):
        """The id-based guard must not make a second reference invisible."""
        shared = _Nested(token=CANARY)
        instance = _Outer(child=shared, children=[shared])
        with pytest.raises(SensitiveValueLeak):
            assert_no_sensitive_values_leak(instance, CANARY)


class TestRenderedShapes:
    def test_mapping_render(self):
        with pytest.raises(SensitiveValueLeak):
            assert_no_sensitive_values_leak(
                _Outer(api_key=CANARY), {"provider": {"api_key": CANARY}}
            )

    def test_list_of_rows_render(self):
        with pytest.raises(SensitiveValueLeak):
            assert_no_sensitive_values_leak(
                _Outer(api_key=CANARY), [("provider", "azure"), ("api_key", CANARY)]
            )

    def test_empty_render(self):
        assert_no_sensitive_values_leak(_Outer(api_key=CANARY), None)


class TestAgainstStackConfig:
    """The shape consumers actually pass: secrets sit two containers deep."""

    def test_model_dump_leaks(self):
        """The canonical failure -- a view built straight off `model_dump()`."""
        stack = _stack_with_secrets()
        with pytest.raises(SensitiveValueLeak, match="password"):
            assert_no_sensitive_values_leak(stack, stack.model_dump())

    def test_api_key_leak_is_caught(self):
        stack = _stack_with_secrets()
        with pytest.raises(SensitiveValueLeak, match="api_key"):
            assert_no_sensitive_values_leak(
                stack, {"providers": {"azure": {"api_key": stack.providers["azure"].api_key}}}
            )

    def test_marker_honouring_render_passes(self):
        """A view that consults `is_sensitive` and `safe_endpoint` is clean."""
        stack = _stack_with_secrets()
        rendered = {
            "connections": {
                name: {
                    field: "***" if is_sensitive(info) else getattr(entry, field)
                    for field, info in type(entry).model_fields.items()
                }
                for name, entry in stack.connections.items()
            },
            "providers": {
                name: {
                    "provider": entry.provider,
                    "base_url": safe_endpoint(entry.base_url),
                    "api_key": "***",
                }
                for name, entry in stack.providers.items()
            },
        }
        assert_no_sensitive_values_leak(stack, rendered)

    def test_safe_url_render_passes(self):
        stack = _stack_with_secrets()
        rendered = [c.safe_url() for c in stack.connections.values()]
        assert_no_sensitive_values_leak(stack, rendered)

    def test_build_url_render_leaks(self):
        """A correctly marked field reaching output through a path that never
        looks at markers -- the case a name-based check cannot see."""
        stack = _stack_with_secrets()
        rendered = [c.build_url() for c in stack.connections.values()]
        with pytest.raises(SensitiveValueLeak, match="password"):
            assert_no_sensitive_values_leak(stack, rendered)
