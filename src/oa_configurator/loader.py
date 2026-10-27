"""Helpers for loading the shared stack configuration from TOML."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import tomllib
from pathlib import Path
from typing import ClassVar

from pydantic import ValidationError

from .package_base import ConfigurationError, StackConfigInvalidError
from .stack_config import StackConfig

logger = logging.getLogger(__name__)

ENV_CONFIG_PATH = "OA_CONFIG_PATH"


def _normalize_path(path: str | Path) -> Path:
    """Expand ``~`` and resolve to an absolute path.

    The one funnel every config-path-accepting entry point (env var, default
    path, explicit ``load``/``save`` callers) passes through, so a caller
    passing a literal ``~/...`` or a relative path can never end up creating
    a directory literally named ``~`` via ``path.parent.mkdir()``.
    """
    return Path(path).expanduser().resolve()


DEFAULT_CONFIG_PATH = _normalize_path("~/.config/omop/config.toml")

_ABSENT_REVISION = "absent"


def _revision_of(raw: bytes) -> str:
    """Return the opaque revision token for the exact bytes *raw*."""
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def stack_config_revision(path: str | Path | None = None) -> str:
    """Return an opaque token identifying the current state of a config file.

    The token changes whenever the file's bytes change, and has a stable value
    while the file is missing. Pass it to
    :func:`~oa_configurator.io.save_stack_config` as ``expected_revision`` to
    save only if the file is still in the state it was read in. The encoding is
    private; compare tokens only for equality.

    To pair a token with a parsed config, use
    :func:`load_stack_config_with_revision`, which reads both from the same
    bytes. This function is for a file that is missing or cannot be parsed.

    Parameters
    ----------
    path : str or pathlib.Path, optional
        File to fingerprint. Omitted uses :func:`active_config_path`.

    Returns
    -------
    str
        The revision token.
    """
    resolved_path = _normalize_path(active_config_path() if path is None else path)
    try:
        return _revision_of(resolved_path.read_bytes())
    except FileNotFoundError:
        return _ABSENT_REVISION


class _ConfigCache:
    """Process-local cache for loaded StackConfig.

    Invalidated automatically by file content changes (mtime + size), so callers
    never need to invalidate it themselves for normal reads. ``OA_CONFIG_PATH``
    is resolved once at module import; changing it within a running process does
    not retarget ``CONFIG_PATH``. Writers
    (:func:`~oa_configurator.io.save_stack_config`) call :meth:`clear`
    directly after writing, as a guard against filesystems with coarse mtime
    resolution.

    Each entry stores the config object actually parsed from disk together
    with the revision of the bytes it was parsed from; callers always receive
    a deep copy so no caller can mutate a shared instance.
    """

    _entries: ClassVar[dict[tuple, tuple[StackConfig, str]]] = {}

    @classmethod
    def _key(cls, resolved_path: Path, st: os.stat_result) -> tuple:
        return (
            resolved_path,
            st.st_mtime_ns,
            st.st_size,
            os.environ.get(ENV_CONFIG_PATH),
        )

    @classmethod
    def get(cls, resolved_path: Path, st: os.stat_result) -> tuple[StackConfig, str] | None:
        cached = cls._entries.get(cls._key(resolved_path, st))
        return (cached[0].model_copy(deep=True), cached[1]) if cached is not None else None

    @classmethod
    def put(
        cls, resolved_path: Path, st: os.stat_result, config: StackConfig, revision: str
    ) -> None:
        cls._entries[cls._key(resolved_path, st)] = (config, revision)

    @classmethod
    def clear(cls) -> None:
        cls._entries.clear()


def _resolve_config_path() -> Path:
    raw = os.environ.get(ENV_CONFIG_PATH)
    if raw:
        p = _normalize_path(raw)
        if not p.exists():
            raise FileNotFoundError(f"{ENV_CONFIG_PATH} points to a non-existent file: {raw!r}")
        if p.suffix != ".toml":
            raise ValueError(f"{ENV_CONFIG_PATH} must point to a .toml file, got: {raw!r}")
        return p
    return DEFAULT_CONFIG_PATH


CONFIG_PATH: Path = _resolve_config_path()


def active_config_path() -> Path:
    """The resolved active config path."""
    return CONFIG_PATH


def invalidate_cache() -> None:
    """Clear the process-local config cache.

    Called by :mod:`~oa_configurator.io` after writing to the config file
    (``save_stack_config``), as a guard against filesystems with coarse
    mtime resolution where a write and the next read could otherwise land
    in the same cache key.
    """
    _ConfigCache.clear()


def load_stack_config(path: str | Path | None = None) -> StackConfig:
    """Load a :class:`StackConfig` from *path*. Defaults to
    :func:`active_config_path`, which honours the ``OA_CONFIG_PATH``
    environment variable.

    Notes
    -----
    The default path is resolved per call, so this always loads whatever
    ``active_config_path()`` currently reports. ``OA_CONFIG_PATH`` itself is
    read once, when this module is first imported.

    Parameters
    ----------
    path : str or pathlib.Path, optional
        Explicit configuration file to load. Omitted uses
        :func:`active_config_path`.

    Returns
    -------
    StackConfig
        Parsed and validated stack configuration.

    Raises
    ------
    FileNotFoundError
        If the resolved path does not exist.
    ConfigurationError
        If the file is not valid TOML. Never echoes the file's contents.
    StackConfigInvalidError
        If the file is valid TOML but does not conform to the StackConfig
        schema. Names the offending fields and never echoes rejected values.
    """
    return load_stack_config_with_revision(path)[0]


def load_stack_config_with_revision(path: str | Path | None = None) -> tuple[StackConfig, str]:
    """Load a :class:`StackConfig` together with the revision of the bytes it was parsed from.

    Reading both from the same bytes guarantees the token describes the file
    the config came from, even if another writer saves in between. Pass the
    token to :func:`~oa_configurator.io.save_stack_config` as
    ``expected_revision`` to refuse saving over changes made since this read.

    Parameters
    ----------
    path : str or pathlib.Path, optional
        Explicit configuration file to load. Omitted uses
        :func:`active_config_path`.

    Returns
    -------
    config : StackConfig
        A deep copy, so a caller can mutate it without disturbing the cache
        or any other holder.
    revision : str
        Opaque token for the loaded bytes, as returned by
        :func:`stack_config_revision`.

    Raises
    ------
    FileNotFoundError
        If the resolved path does not exist.
    ConfigurationError
        If the file is not valid TOML. Never echoes the file's contents.
    StackConfigInvalidError
        If the file is valid TOML but does not conform to the StackConfig
        schema. Names the offending fields and never echoes rejected values.
    """
    resolved_path = _normalize_path(active_config_path() if path is None else path)

    if not resolved_path.exists():
        raise FileNotFoundError(f"Config file not found: {resolved_path}")

    st = resolved_path.stat()
    file_mode = stat.S_IMODE(st.st_mode)
    if file_mode & 0o044:
        logger.warning(
            "Config file %s has loose permissions (mode %04o). "
            "It may contain passwords. Run 'chmod 600 %s' to restrict access.",
            resolved_path,
            file_mode,
            resolved_path,
        )

    cached = _ConfigCache.get(resolved_path, st)
    if cached is not None:
        return cached

    raw = resolved_path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        # tomllib reports a position ("at line 2, column 9"), never the offending
        # text, so a malformed line holding a password cannot echo it here.
        raise ConfigurationError(f"Malformed TOML in {resolved_path}: {exc}") from exc

    try:
        config = StackConfig.model_validate(data)
    except ValidationError as exc:
        raise StackConfigInvalidError(resolved_path, exc) from None
    config.bind_loaded_path(resolved_path)

    revision = _revision_of(raw)
    _ConfigCache.put(resolved_path, st, config, revision)
    return config.model_copy(deep=True), revision
