"""Runtime settings for locating and lightly overriding stack configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_CONFIG_PATH = Path("~/.config/omop/config.toml").expanduser()


class RuntimeSettings(BaseSettings):
    """Environment-backed runtime knobs for config loading.

    Only ``active_profile`` is configurable via environment (``OA_ACTIVE_PROFILE``).
    The config file path is always ``DEFAULT_CONFIG_PATH`` (``~/.config/omop/config.toml``)
    and cannot be overridden — keeping a fixed, well-known location ensures every
    package can find the config without coordination.
    """

    active_profile: str | None = None

    model_config = SettingsConfigDict(
        env_prefix="OA_",
        env_file=".env",
        extra="ignore",
    )
