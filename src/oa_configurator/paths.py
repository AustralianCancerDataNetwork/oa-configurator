"""Internal helpers for consistent filesystem path resolution."""

from __future__ import annotations

from pathlib import Path


def resolve_filesystem_path(value: str | Path, configuration_base_path: Path | None = None) -> Path:
    """Resolve a filesystem path with optional configuration-relative semantics."""

    expanded = Path(value).expanduser()
    if expanded.is_absolute() or configuration_base_path is None:
        return expanded.resolve()
    return (configuration_base_path / expanded).resolve()


def display_path(value: Path | None) -> str | None:
    """Render a resolved path as a plain string for repr output."""

    if value is None:
        return None
    return str(value)
