"""Structured, renderer-agnostic configuration issues.

Resolution and planning raise :class:`ConfigIssuesError` instead of printing,
so headless callers (TUIs, services) can read ``issues`` directly while the
CLI renders the same issues as Rich output.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from pydantic_core import ErrorDetails


class ConfigurationError(ValueError):
    """General-purpose config-error type for this package.

    Raised across many distinct situations, not just a missing database or
    connection:
    - malformed stack-config TOML,
    - a missing required CLI field,
    - an unrecognized ``--set``/flag key,
    - a failed entry validation,
    - a test/production connection collision,
    - an unresolved or kind-mismatched ref,
    - an ``is_test``/``test_only`` mismatch, and a headless ``plan_configure`` failure,
    - ...
    """


class Severity(StrEnum):
    """How serious an issue is."""

    ERROR = "error"
    WARNING = "warning"


class IssueCode(StrEnum):
    """Stable identifiers for the kinds of issue resolution can report."""

    MISSING_REQUIRED = "missing_required"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_ENTRY = "invalid_entry"
    TEST_COLLISION = "test_collision"
    COLLISION_UNVERIFIABLE = "collision_unverifiable"
    TEST_ONLY_MISMATCH = "test_only_mismatch"


@dataclass(frozen=True)
class ConfigIssue:
    """One problem found while resolving or planning a configuration.

    Parameters
    ----------
    code : IssueCode
        Stable identifier of the problem kind.
    message : str
        Human-readable description, safe to log.
    severity : Severity
        Whether the issue blocks the operation.
    package : str or None
        Tool name of the package section involved.
    section : str or None
        Stack section of the entry involved, e.g. ``"databases"``.
    entry : str or None
        Name of the entry involved.
    schema : str or None
        Class name of the entry schema involved.
    field_path : str or None
        Dotted path of the field involved. ``None`` for an entry-level issue,
        such as a cross-field validator failure.
    related : tuple of str
        Names that help act on the issue: the valid field names for
        ``UNKNOWN_FIELD``, the colliding connection name for ``TEST_COLLISION``.
    error_type : str or None
        Pydantic error type of an underlying validation failure.
    """

    code: IssueCode
    message: str
    severity: Severity = Severity.ERROR
    package: str | None = None
    section: str | None = None
    entry: str | None = None
    schema: str | None = None
    field_path: str | None = None
    related: tuple[str, ...] = ()
    error_type: str | None = None

    @property
    def subject(self) -> str | None:
        """Label for the package or entry involved, or ``None`` if unknown."""
        if self.package is not None:
            return f"tool {self.package!r}"
        if self.entry is not None:
            kind = self.section[:-1] if self.section else "entry"
            return f"{kind} {self.entry!r}"
        return None


class ConfigIssuesError(ConfigurationError):
    """Resolution or planning failed with one or more structured issues.

    Parameters
    ----------
    message : str
        Summary of the failure.
    issues : iterable of ConfigIssue
        The problems found. All share one code.

    Attributes
    ----------
    issues : tuple of ConfigIssue
        The problems found.
    """

    def __init__(self, message: str, issues: Iterable[ConfigIssue]) -> None:
        super().__init__(message)
        self.issues = tuple(issues)


def issues_from_errors(
    errors: Iterable[ErrorDetails],
    *,
    package: str | None = None,
    schema: str | None = None,
) -> list[ConfigIssue]:
    """Build one ``INVALID_ENTRY`` issue per sanitized pydantic error.

    Parameters
    ----------
    errors : iterable of ErrorDetails
        Errors already passed through :func:`~oa_configurator.refs.sanitized_errors`.
    package : str or None
        Tool name of the package section that failed.
    schema : str or None
        Class name of the entry schema that failed.

    Returns
    -------
    list of ConfigIssue
        Issues whose ``field_path`` is ``None`` for errors with no field
        location, such as cross-field model validators.
    """
    return [
        ConfigIssue(
            IssueCode.INVALID_ENTRY,
            error["msg"].removeprefix("Value error, "),
            package=package,
            schema=schema,
            field_path=".".join(str(part) for part in error["loc"]) or None,
            error_type=error["type"],
        )
        for error in errors
    ]
