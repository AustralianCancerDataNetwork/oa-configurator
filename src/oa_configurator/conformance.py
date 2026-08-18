"""Test-time assertions that a package honours the ``Sensitive()`` declaration."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .refs import is_sensitive


class SensitiveValueLeak(AssertionError):
    """A value declared ``Sensitive()`` appeared in rendered output."""


def assert_no_sensitive_values_leak(instance: BaseModel, rendered: object) -> None:
    """Assert that no ``Sensitive()`` value of *instance* appears in *rendered*.

    Give the secret fields a distinctive canary value before rendering. The
    check is a substring search, so a one-character password matches almost
    any output and a password of ``"postgres"`` matches the dialect.

    Parameters
    ----------
    instance : pydantic.BaseModel
        The configuration the rendering was produced from. Usually a
        ``StackConfig``, but any model works.
    rendered : object
        Whatever the package produced for display, logging, or serialisation.
        Converted with ``str()``, so a mapping, dataclass, list of view rows,
        or already-formatted string are all acceptable.

    Raises
    ------
    SensitiveValueLeak
        If any non-empty sensitive value appears in the rendered output. The
        message names the field's path within *instance*; it never repeats
        the leaked value, since assertion messages end up in CI logs.
    """
    haystack = str(rendered)
    for path, value in _sensitive_values(instance, type(instance).__name__, set()):
        if value in haystack:
            raise SensitiveValueLeak(
                f"{path} is declared Sensitive() but its value appears in the "
                f"rendered output. Either the rendering path does not consult "
                f"is_sensitive(), or it reads the value from somewhere that "
                f"has already dropped the marker (a model_dump(), a resolved "
                f"dataclass, an f-string over the raw config)."
            )


def _sensitive_values(
    model: BaseModel, path: str, seen: set[int]
) -> list[tuple[str, str]]:
    """Collect ``(dotted_path, value)`` for every sensitive value under *model*."""
    if id(model) in seen:
        return []
    seen.add(id(model))
    found: list[tuple[str, str]] = []
    for name, info in type(model).model_fields.items():
        value = getattr(model, name, None)
        child_path = f"{path}.{name}"
        if is_sensitive(info) and value is not None and str(value) != "":
            found.append((child_path, str(value)))
        found.extend(_walk(value, child_path, seen))
    return found


def _walk(value: Any, path: str, seen: set[int]) -> list[tuple[str, str]]:
    if isinstance(value, BaseModel):
        return _sensitive_values(value, path, seen)
    if isinstance(value, dict):
        return [
            found
            for key, item in value.items()
            for found in _walk(item, f"{path}[{key!r}]", seen)
        ]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            found
            for index, item in enumerate(value)
            for found in _walk(item, f"{path}[{index}]", seen)
        ]
    return []
