"""Helpers for loading the shared stack configuration from TOML."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from .models import StackConfig

DEFAULT_CONFIG_PATH = Path("~/.config/omop/config.toml").expanduser()


def load_stack_config() -> StackConfig:
    """Load a :class:`StackConfig` from ``DEFAULT_CONFIG_PATH``
    (``~/.config/omop/config.toml``).

    The active profile can be overridden via the ``OA_ACTIVE_PROFILE``
    environment variable without modifying the file.

    Raises
    ------
    FileNotFoundError
        If ``DEFAULT_CONFIG_PATH`` does not exist.
    """
    return _load_from_path(DEFAULT_CONFIG_PATH)


def _load_from_path(path: str | Path) -> StackConfig:
    """Load a :class:`StackConfig` from an explicit path.

    Intended for CLI commands and tooling. Application code should use
    :func:`load_stack_config` instead.
    """
    resolved_path = Path(path).expanduser()

    if not resolved_path.exists():
        raise FileNotFoundError(f"Config file not found: {resolved_path}")

    try:
        data = tomllib.loads(resolved_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Malformed TOML in {resolved_path}: {exc}") from exc

    config = StackConfig.model_validate(data)

    active_profile = os.environ.get("OA_ACTIVE_PROFILE")
    if active_profile:
        config.active_profile = active_profile

    config.bind_loaded_path(resolved_path)

    return config
