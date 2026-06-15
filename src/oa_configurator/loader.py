"""Helpers for loading the shared stack configuration from TOML."""

from __future__ import annotations

import logging
import os
import stat
import tomllib
from pathlib import Path

from .models import StackConfig

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("~/.config/omop/config.toml").expanduser()
ENV_ACTIVE_PROFILE = "OA_ACTIVE_PROFILE"
ENV_CONFIG_PATH = "OA_CONFIG_PATH"


def _resolve_config_path() -> Path:
    raw = os.environ.get(ENV_CONFIG_PATH)
    if raw:
        p = Path(raw).expanduser()
        if p.suffix != ".toml":
            raise ValueError(
                f"{ENV_CONFIG_PATH} must point to a .toml file, got: {raw!r}"
            )
        return p
    return DEFAULT_CONFIG_PATH


CONFIG_PATH: Path = _resolve_config_path()


def load_stack_config() -> StackConfig:
    """Load a :class:`StackConfig` from ``CONFIG_PATH``
    (default ``~/.config/omop/config.toml``, overridable via ``OA_CONFIG_PATH``).

    The active profile can be overridden via the ``OA_ACTIVE_PROFILE``
    environment variable without modifying the file.

    Raises
    ------
    FileNotFoundError
        If ``CONFIG_PATH`` does not exist.
    """
    return _load_from_path(CONFIG_PATH)


def _load_from_path(path: str | Path) -> StackConfig:
    """Load a :class:`StackConfig` from an explicit path.

    Intended for CLI commands and tooling. Application code should use
    :func:`load_stack_config` instead.
    """
    resolved_path = Path(path).expanduser()

    if not resolved_path.exists():
        raise FileNotFoundError(f"Config file not found: {resolved_path}")

    file_mode = stat.S_IMODE(resolved_path.stat().st_mode)
    if file_mode & 0o044:
        logger.warning(
            "Config file %s has loose permissions (mode %04o). "
            "It may contain passwords. Run 'chmod 600 %s' to restrict access.",
            resolved_path, file_mode, resolved_path,
        )

    try:
        data = tomllib.loads(resolved_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Malformed TOML in {resolved_path}: {exc}") from exc

    config = StackConfig.model_validate(data)

    active_profile = os.environ.get(ENV_ACTIVE_PROFILE)
    if active_profile:
        config.active_profile = active_profile

    config.bind_loaded_path(resolved_path)

    return config
