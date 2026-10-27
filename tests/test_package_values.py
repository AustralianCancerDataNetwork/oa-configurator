"""Defaults, explicit values and unknown keys in a package's ``[tools.*]`` section."""

from __future__ import annotations

from typing import ClassVar

import pytest
import typer

from oa_configurator import (
    PackageConfigBase,
    PackagePlan,
    StackConfig,
    plan_configure,
    plan_package_config,
    prune_unknown_keys,
)
from oa_configurator.refs import Secret

_SECRET = "canary-9a3e17-do-not-render"


class ValuesConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "values_tool"
    port: int = 8000
    host: str = "localhost"
    verbose: bool = False
    token: Secret = None


def _stack(**section: object) -> StackConfig:
    return StackConfig.for_session(tools={"values_tool": dict(section)})


class TestDefaultsAreNotStored:
    def test_empty_plan_stores_nothing(self):
        planned = plan_configure(ValuesConfig, StackConfig.for_session(), {})
        assert planned.tools["values_tool"] == {}

    def test_stored_value_is_kept_and_defaults_stay_out(self):
        planned = plan_configure(ValuesConfig, _stack(port=9000), {})
        assert planned.tools["values_tool"] == {"port": 9000}

    def test_value_equal_to_default_is_stored_when_supplied(self):
        planned = plan_configure(ValuesConfig, StackConfig.for_session(), {"host": "localhost"})
        assert planned.tools["values_tool"] == {"host": "localhost"}

    def test_supplied_value_is_normalized(self):
        planned = plan_configure(ValuesConfig, StackConfig.for_session(), {"port": "9001"})
        assert planned.tools["values_tool"] == {"port": 9001}

    def test_plan_lists_defaulted_fields(self):
        plan = plan_package_config(ValuesConfig, _stack(port=9000), {"host": "db"})
        assert isinstance(plan, PackagePlan)
        assert plan.defaulted_fields == ("verbose", "token")

    def test_running_package_still_sees_the_defaults(self):
        planned = plan_configure(ValuesConfig, StackConfig.for_session(), {})
        from oa_configurator import Resolver

        resolved = Resolver(planned).resolve_package_config(ValuesConfig)
        assert (resolved.port, resolved.host) == (8000, "localhost")


class TestUnknownKeysArePreserved:
    def test_unknown_keys_survive_and_are_reported(self):
        plan = plan_package_config(
            ValuesConfig, _stack(port=9000, legacy="x", api_secret=_SECRET), {"host": "db"}
        )
        assert plan.config.tools["values_tool"] == {
            "api_secret": _SECRET,
            "legacy": "x",
            "host": "db",
            "port": 9000,
        }
        assert plan.unknown_fields == ("api_secret", "legacy")

    def test_report_never_contains_values(self):
        plan = plan_package_config(ValuesConfig, _stack(api_secret=_SECRET), {})
        assert _SECRET not in repr(plan)

    def test_input_stack_is_unchanged(self):
        config = _stack(legacy="x")
        plan_configure(ValuesConfig, config, {"port": 1})
        assert config.tools["values_tool"] == {"legacy": "x"}

    def test_no_unknown_keys_reports_none(self):
        assert plan_package_config(ValuesConfig, _stack(port=1), {}).unknown_fields == ()


class TestPrune:
    def test_removes_only_unknown_keys(self):
        config = _stack(port=9000, legacy="x", api_secret=_SECRET)
        report = prune_unknown_keys(config, [ValuesConfig])
        assert report.config.tools["values_tool"] == {"port": 9000}
        assert report.removed == {"values_tool": ("api_secret", "legacy")}
        assert _SECRET not in repr(report)

    def test_input_is_unchanged(self):
        config = _stack(legacy="x")
        prune_unknown_keys(config, [ValuesConfig])
        assert config.tools["values_tool"] == {"legacy": "x"}

    def test_sections_without_a_class_are_left_alone(self):
        config = StackConfig.for_session(tools={"other": {"anything": 1}})
        report = prune_unknown_keys(config, [ValuesConfig])
        assert report.config.tools == {"other": {"anything": 1}}
        assert report.removed == {}

    def test_clean_stack_reports_nothing(self):
        report = prune_unknown_keys(_stack(port=1), [ValuesConfig])
        assert report.removed == {}


class TestPrompts:
    """Enter leaves a field alone; typing a value, even the default, stores it."""

    @staticmethod
    def _answer(monkeypatch, answers: dict[str, str]) -> list[str]:
        seen: list[str] = []

        def prompt(text, default="", **kwargs):
            seen.append(text)
            for name, answer in answers.items():
                if text.startswith(name):
                    return answer
            return default

        monkeypatch.setattr(typer, "prompt", prompt)
        return seen

    def _resolve(self, config: StackConfig | None = None) -> dict:
        return ValuesConfig.resolve_fields(
            config or StackConfig.for_session(), set_dict={}, interactive=True
        )

    def test_enter_on_unset_fields_stores_nothing(self, monkeypatch):
        self._answer(monkeypatch, {})
        assert self._resolve() == {}

    def test_typing_the_default_stores_it(self, monkeypatch):
        self._answer(monkeypatch, {"host": "localhost"})
        assert self._resolve() == {"host": "localhost"}

    def test_enter_keeps_a_stored_value(self, monkeypatch):
        self._answer(monkeypatch, {})
        assert self._resolve(_stack(host="db", verbose=True)) == {"host": "db", "verbose": True}

    def test_label_names_what_enter_leaves_in_effect(self, monkeypatch):
        seen = self._answer(monkeypatch, {})
        self._resolve(_stack(host="db"))
        assert any(t.startswith("host") and t.endswith("[Enter = db]") for t in seen)
        assert any(t.startswith("port") and t.endswith("[Enter = 8000]") for t in seen)

    def test_sensitive_value_is_not_shown(self, monkeypatch):
        seen = self._answer(monkeypatch, {})
        self._resolve(_stack(token=_SECRET))
        assert not any(_SECRET in t for t in seen)

    def test_bool_enter_leaves_it_unset(self, monkeypatch):
        self._answer(monkeypatch, {})
        assert "verbose" not in self._resolve()

    @pytest.mark.parametrize(("answer", "expected"), [("y", True), ("n", False)])
    def test_bool_answer_is_stored_even_when_equal_to_default(self, monkeypatch, answer, expected):
        self._answer(monkeypatch, {"verbose": answer})
        assert self._resolve()["verbose"] is expected

    def test_bool_invalid_answer_asks_again(self, monkeypatch):
        replies = iter(["maybe", "y"])
        monkeypatch.setattr(
            typer,
            "prompt",
            lambda text, default="", **kw: next(replies) if text.startswith("verbose") else default,
        )
        assert self._resolve()["verbose"] is True
