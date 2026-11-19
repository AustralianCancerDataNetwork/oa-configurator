"""Context applied to structured issues by the code that knows where they occurred."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from oa_configurator import ConfigIssue, ConfigIssuesError, IssueCode
from oa_configurator.issues import issue_context
from oa_configurator.package_base import PackageConfigBase, PackageConfigInvalidError


def _issue(**kwargs) -> ConfigIssue:
    return ConfigIssue(IssueCode.INVALID_ENTRY, "bad", **kwargs)


class TestWithin:
    def test_field_is_prepended_to_the_path(self):
        assert _issue(field_path="port").within(field="db").field_path == "db.port"

    def test_entry_level_issue_gets_the_field_as_its_path(self):
        assert _issue().within(field="db").field_path == "db"

    def test_context_already_set_is_kept(self):
        issue = _issue(package="inner", section="models", entry="m").within(
            package="outer", section="databases", entry="d"
        )
        assert (issue.package, issue.section, issue.entry) == ("inner", "models", "m")

    def test_missing_context_is_filled(self):
        issue = _issue().within(package="tool", section="databases", entry="d")
        assert (issue.package, issue.section, issue.entry) == ("tool", "databases", "d")


class TestIssueContext:
    def test_nested_contexts_build_the_full_path(self):
        with (
            pytest.raises(ConfigIssuesError) as exc_info,
            issue_context(package="tool"),
            issue_context(field="outer"),
            issue_context(field="inner"),
        ):
            raise ConfigIssuesError("x", [_issue(field_path="leaf")])

        (issue,) = exc_info.value.issues
        assert issue.field_path == "outer.inner.leaf"
        assert issue.package == "tool"

    def test_subclass_and_traceback_are_preserved(self):
        class Strict(PackageConfigBase):
            tool_name = "strict"
            port: int = 1

        with pytest.raises(ValidationError) as validation:
            Strict.model_validate({"port": "x"})
        error = PackageConfigInvalidError("strict", validation.value)

        with pytest.raises(PackageConfigInvalidError) as exc_info, issue_context(field="f"):
            raise error

        assert exc_info.value is error
        assert exc_info.value.issues[0].field_path == "f.port"

    def test_other_exceptions_pass_through(self):
        with pytest.raises(ValueError, match="boom"), issue_context(field="f"):
            raise ValueError("boom")
