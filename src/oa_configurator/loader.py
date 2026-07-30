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


class _ConfigCache:
    """Process-local cache for loaded StackConfig.

    Invalidated automatically by file content changes (mtime + size) and by
    the env vars that affect parsing (OA_ACTIVE_PROFILE, OA_CONFIG_PATH), so
    callers never need to invalidate it themselves for normal reads. Writers
    (:func:`~oa_configurator.io.save_stack_config`,
    :func:`~oa_configurator.io.patch_active_profile`) call :meth:`clear`
    directly after writing, as a guard against filesystems with coarse mtime
    resolution.

    Each entry stores the config object actually parsed from disk; callers
    always receive a deep copy so no caller can mutate a shared instance.
    """

    _entries: dict[tuple, StackConfig] = {}

    @classmethod
    def _key(cls, resolved_path: Path, st: os.stat_result) -> tuple:
        return (
            resolved_path,
            st.st_mtime_ns,
            st.st_size,
            os.environ.get(ENV_ACTIVE_PROFILE),
            os.environ.get(ENV_CONFIG_PATH),
        )

    @classmethod
    def get(cls, resolved_path: Path, st: os.stat_result) -> StackConfig | None:
        cached = cls._entries.get(cls._key(resolved_path, st))
        return cached.model_copy(deep=True) if cached is not None else None

    @classmethod
    def put(cls, resolved_path: Path, st: os.stat_result, config: StackConfig) -> None:
        cls._entries[cls._key(resolved_path, st)] = config

    @classmethod
    def clear(cls) -> None:
        cls._entries.clear()


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


def invalidate_cache() -> None:
    """Clear the process-local config cache.

    Called by :mod:`~oa_configurator.io` after writing to the config file
    (``save_stack_config``, ``patch_active_profile``), as a guard against
    filesystems with coarse mtime resolution where a write and the next
    read could otherwise land in the same cache key.
    """
    _ConfigCache.clear()


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

    st = resolved_path.stat()
    file_mode = stat.S_IMODE(st.st_mode)
    if file_mode & 0o044:
        logger.warning(
            "Config file %s has loose permissions (mode %04o). "
            "It may contain passwords. Run 'chmod 600 %s' to restrict access.",
            resolved_path, file_mode, resolved_path,
        )

    cached = _ConfigCache.get(resolved_path, st)
    if cached is not None:
        return cached

    try:
        data = tomllib.loads(resolved_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Malformed TOML in {resolved_path}: {exc}") from exc

    config = StackConfig.model_validate(data)

    active_profile = os.environ.get(ENV_ACTIVE_PROFILE)
    if active_profile:
        config.active_profile = active_profile

    config.bind_loaded_path(resolved_path)

    _ConfigCache.put(resolved_path, st, config)
    return config.model_copy(deep=True)
