"""Helpers for loading the shared stack configuration from TOML."""

from __future__ import annotations

from pathlib import Path
import tomllib

from .models import StackConfig
from .settings import DEFAULT_CONFIG_PATH, RuntimeSettings


def load_stack_config() -> StackConfig:
    """Load a :class:`StackConfig` from ``~/.config/omop/config.toml``.

    The config path is always ``DEFAULT_CONFIG_PATH`` and cannot be
    overridden at runtime. Use ``StackConfig.for_session()`` or
    ``StackConfig.model_validate(tomllib.loads(...))`` in tests and
    scripts that need a different file.

    The active profile can be overridden via the ``OA_ACTIVE_PROFILE``
    environment variable without changing the file path.

    Returns
    -------
    StackConfig
        Parsed and validated stack configuration.

    Raises
    ------
    FileNotFoundError
        If ``~/.config/omop/config.toml`` does not exist.
    """
    return _load_from_path(DEFAULT_CONFIG_PATH)


def _load_from_path(path: str | Path) -> StackConfig:
    """Load a :class:`StackConfig` from an explicit path.

    Intended for CLI commands and tooling that inspect or edit a specific
    file. Application code should use :func:`load_stack_config` instead.
    """
    runtime = RuntimeSettings()
    resolved_path = Path(path).expanduser()

    if not resolved_path.exists():
        raise FileNotFoundError(f"Config file not found: {resolved_path}")

    try:
        data = tomllib.loads(resolved_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Malformed TOML in {resolved_path}: {exc}") from exc

    config = StackConfig.model_validate(data)

    if runtime.active_profile is not None:
        config.active_profile = runtime.active_profile

    config.bind_loaded_path(resolved_path)

    return config
