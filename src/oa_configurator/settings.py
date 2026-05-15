"""Runtime settings for locating and lightly overriding stack configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_CONFIG_PATH = Path("~/.config/omop/config.toml").expanduser()


class RuntimeSettings(BaseSettings):
    """Environment-backed runtime knobs for config loading.

    These settings are intentionally small and operational:

    - where the main TOML file lives
    - which profile to force
    - which logical stack to force

    They are not intended to replace the main TOML configuration model.
    """

    config_file: Path = DEFAULT_CONFIG_PATH
    active_profile: str | None = None
    active_stack: str | None = None

    model_config = SettingsConfigDict(
        env_prefix="OA_",
        env_file=".env",
        extra="ignore",
    )
