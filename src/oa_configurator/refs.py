"""Deprecated import path for names that moved to :mod:`oa_configurator.references` and :mod:`oa_configurator.sensitive`."""

from __future__ import annotations

import warnings
from importlib import import_module
from typing import Any

_MOVED = {
    "RefTo": "references",
    "_iter_refs": "references",
    "MASK": "sensitive",
    "Secret": "sensitive",
    "SecretSafeBaseModel": "sensitive",
    "Sensitive": "sensitive",
    "describe_errors": "sensitive",
    "is_sensitive": "sensitive",
    "safe_endpoint": "sensitive",
    "sanitized_errors": "sensitive",
}


def __getattr__(name: str) -> Any:
    module = _MOVED.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"oa_configurator.refs.{name} moved to oa_configurator.{module}.{name}; "
        "oa_configurator.refs will be removed in the next major release.",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(import_module(f"{__package__}.{module}"), name)


def __dir__() -> list[str]:
    return sorted(_MOVED)
