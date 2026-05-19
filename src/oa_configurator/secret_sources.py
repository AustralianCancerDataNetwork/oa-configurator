"""Internal helpers for resolving secret values from supported sources."""

from __future__ import annotations

import os
from pathlib import Path

from .paths import resolve_filesystem_path


class SecretSourceResolutionError(RuntimeError):
    """Raised when a configured secret source cannot be resolved."""


def resolve_secret_value(
    secret_source: str,
    *,
    configuration_base_path: Path,
    secrets_dir: Path | None = None,
) -> str:
    """Resolve one supported secret source into its plaintext value.

    Supported formats are:

    - ``env:VARIABLE_NAME``
    - ``file:relative/or/absolute/path``
    """

    source_kind, separator, source_value = secret_source.partition(":")
    if separator == "" or source_value == "":
        raise SecretSourceResolutionError(
            "secret_source must use the form 'env:NAME' or 'file:PATH'"
        )

    if source_kind == "env":
        secret_value = os.environ.get(source_value)
        if secret_value is None:
            raise SecretSourceResolutionError(
                f"environment variable {source_value!r} is not set"
            )
        if secret_value == "":
            raise SecretSourceResolutionError(
                f"environment variable {source_value!r} is set but empty"
            )
        return secret_value

    if source_kind == "file":
        file_base = secrets_dir or configuration_base_path
        secret_path = resolve_filesystem_path(source_value, file_base)
        if not secret_path.exists():
            raise SecretSourceResolutionError(f"secret file not found: {secret_path}")
        if not secret_path.is_file():
            raise SecretSourceResolutionError(f"secret path is not a file: {secret_path}")
        return secret_path.read_text(encoding="utf-8").rstrip("\r\n")

    raise SecretSourceResolutionError(
        f"unsupported secret source kind {source_kind!r}; expected 'env' or 'file'"
    )
