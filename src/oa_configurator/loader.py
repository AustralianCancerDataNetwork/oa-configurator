"""Helpers for loading the shared stack configuration from TOML."""

from __future__ import annotations

from pathlib import Path
import tomllib

from .models import StackConfig
from .settings import DEFAULT_CONFIG_PATH, RuntimeSettings


def load_stack_config(path: str | Path | None = None) -> StackConfig:
    """Load a :class:`StackConfig` from TOML and apply runtime overrides.

    Parameters
    ----------
    path
        Optional explicit path to the configuration file. When omitted,
        ``RuntimeSettings.config_file`` is used.

    Returns
    -------
    StackConfig
        Parsed and validated stack configuration.

    Raises
    ------
    FileNotFoundError
        If the resolved configuration file does not exist.
    """

    runtime = RuntimeSettings()
    resolved_path = Path(path or runtime.config_file).expanduser()

    if not resolved_path.exists():
        raise FileNotFoundError(f"Config file not found: {resolved_path}")

    data = tomllib.loads(resolved_path.read_text(encoding="utf-8"))
    config = StackConfig.model_validate(data)

    if runtime.active_profile is not None:
        config.settings.active_profile = runtime.active_profile
    if runtime.active_stack is not None:
        config.settings.active_stack = runtime.active_stack

    config.bind_loaded_path(resolved_path)

    return config
