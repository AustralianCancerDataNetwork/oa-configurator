"""Persistence helpers for reading and writing TOML configuration files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomli_w

from .models import StackConfig


def save_stack_config(config: StackConfig, path: Path) -> Path:
    """Serialize and write a validated :class:`StackConfig` back to TOML.

    The writer currently normalizes output rather than preserving comments or
    original formatting. That is acceptable for the current prototype CLI,
    where correctness and predictability are more important than round-trip
    fidelity.
    """

    payload = _drop_none_and_empty(config.model_dump(mode="python"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(payload), encoding="utf-8")
    return path


def _drop_none_and_empty(value: Any) -> Any:
    """Recursively remove ``None`` values and empty dictionaries."""

    if isinstance(value, dict):
        cleaned = {
            key: _drop_none_and_empty(inner)
            for key, inner in value.items()
            if inner is not None
        }
        return {
            key: inner
            for key, inner in cleaned.items()
            if inner != {}
        }
    if isinstance(value, list):
        return [_drop_none_and_empty(inner) for inner in value]
    return value
