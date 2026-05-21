"""Helpers for writing configuration back to TOML."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from .logging_config import LoggingConfig
from .models import StackConfig
from .settings import DEFAULT_CONFIG_PATH


def save_stack_config(config: StackConfig, path: Path = DEFAULT_CONFIG_PATH) -> Path:
    """Serialize a :class:`StackConfig` back to TOML.

    Does NOT preserve comments or original formatting. The ``logging`` section
    is omitted when it equals the default (no custom logging configured).
    """
    payload = _drop_none_and_empty(config.model_dump(mode="python"))
    if config.logging == LoggingConfig():
        payload.pop("logging", None)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(payload), encoding="utf-8")
    return path


def patch_active_profile(profile_name: str, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Set ``active_profile`` in the TOML file without touching other fields.

    If the file does not exist it is created with only the ``active_profile``
    key. Used by ``omop-config use <profile>``.
    """
    data: dict[str, Any] = {}
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    data["active_profile"] = profile_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


def _drop_none_and_empty(value: Any) -> Any:
    """Recursively strip None values and empty dicts from a model_dump result."""
    if isinstance(value, dict):
        cleaned = {k: _drop_none_and_empty(v) for k, v in value.items() if v is not None}
        return {k: v for k, v in cleaned.items() if v != {}}
    if isinstance(value, list):
        return [_drop_none_and_empty(v) for v in value]
    return value
