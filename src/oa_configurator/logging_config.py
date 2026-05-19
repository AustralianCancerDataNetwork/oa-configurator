"""Logging configuration and application for the OMOP stack."""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

STACK_LOG_NAMESPACES: tuple[str, ...] = (
    "oa_configurator",
    "orm_loader",
    "sql_loader",    # orm-loader legacy namespace; both configured during transition
    "omop_alchemy",
    "omop_emb",
    "omop_graph",
)

_PRESET_LEVELS: dict[str, str] = {
    "library": "WARNING",
    "notebook": "INFO",
    "application": "INFO",
    "production": "INFO",
}

_SIMPLE_FORMAT = "%(levelname)-8s %(name)s: %(message)s"
_DETAILED_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"

_SENSITIVE_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|key|dsn|uri|url)\b\s*[:=]\s*\S+",
)


class _RedactingFormatter(logging.Formatter):
    """Formatter that redacts sensitive key=value pairs from log output."""

    def format(self, record: logging.LogRecord) -> str:
        return _SENSITIVE_PATTERN.sub(r"\1=<REDACTED>", super().format(record))


class _JsonFormatter(logging.Formatter):
    """Newline-delimited JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": _SENSITIVE_PATTERN.sub(r"\1=<REDACTED>", record.getMessage()),
        }
        if record.exc_info:
            entry["exc_info"] = _SENSITIVE_PATTERN.sub(
                r"\1=<REDACTED>",
                self.formatException(record.exc_info),
            )
        return json.dumps(entry)


def _coerce_level(value: str) -> str:
    """Normalise a level string; raise ValueError for unknown values."""
    upper = value.strip().upper()
    if upper.isdigit():
        if int(upper) not in {0, 10, 20, 30, 40, 50}:
            raise ValueError(
                f"Unknown numeric log level: {value!r}. "
                "Use 0, 10, 20, 30, 40, or 50."
            )
        return upper
    if not hasattr(logging, upper):
        raise ValueError(
            f"Unknown log level: {value!r}. "
            "Use DEBUG, INFO, WARNING, ERROR, or CRITICAL."
        )
    return upper


class _HasLoggingConfig(Protocol):
    """Minimal protocol for StackConfig-like logging configuration objects."""

    logging: "LoggingConfig"
    config_file_path: Path | None


class LoggingHandlerConfig(BaseModel):
    """Where and how to write log records."""

    model_config = ConfigDict(extra="forbid")

    target: Literal["stderr", "stdout", "file"] = "stderr"
    format: Literal["simple", "detailed", "json"] = "detailed"
    file_path: str | None = None

    def __repr__(self) -> str:
        return (
            "LoggingHandlerConfig("
            f"target={self.target!r}, "
            f"format={self.format!r}, "
            f"file_path={self.file_path!r}"
            ")"
        )


class LoggingConfig(BaseModel):
    """Logging configuration for the OMOP stack.

    The ``preset`` selects sensible defaults for level, handler, and format:

    - ``library`` (default): WARNING level, no handler. Safe for library use —
      never touches the caller's logging setup.
    - ``notebook``: INFO level, stdout, simple single-line format (no timestamps).
    - ``application``: INFO level, stderr, detailed format with timestamps.
    - ``production``: INFO level, stdout, newline-delimited JSON.

    ``level`` and ``handler`` override the preset's defaults when set.
    ``loggers`` applies fine-grained level overrides to any named logger,
    including third-party ones such as ``sqlalchemy.engine``.
    """

    model_config = ConfigDict(extra="forbid")

    preset: Literal["library", "notebook", "application", "production"] = "library"
    level: str | None = None
    loggers: dict[str, str] = Field(default_factory=dict)
    handler: LoggingHandlerConfig | None = None

    @field_validator("level")
    @classmethod
    def _validate_level(cls, v: str | None) -> str | None:
        return None if v is None else _coerce_level(v)

    @field_validator("loggers")
    @classmethod
    def _validate_loggers(cls, v: dict[str, str]) -> dict[str, str]:
        return {name: _coerce_level(level) for name, level in v.items()}

    def __repr__(self) -> str:
        return (
            "LoggingConfig("
            f"preset={self.preset!r}, "
            f"level={self.level!r}, "
            f"loggers={sorted(self.loggers)!r}"
            ")"
        )


_PRESET_HANDLER_DEFAULTS: dict[str, LoggingHandlerConfig | None] = {
    "library": None,
    "notebook": LoggingHandlerConfig(target="stdout", format="simple"),
    "application": LoggingHandlerConfig(target="stderr", format="detailed"),
    "production": LoggingHandlerConfig(target="stdout", format="json"),
}


def _make_handler(
    config: LoggingHandlerConfig,
    *,
    base_path: Path | None = None,
) -> logging.Handler:
    if config.target == "file":
        if config.file_path is None:
            raise ValueError(
                "LoggingHandlerConfig.file_path is required when target='file'."
            )
        path = Path(config.file_path)
        if not path.is_absolute() and base_path is not None:
            path = base_path / path
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(path, encoding="utf-8")
    elif config.target == "stdout":
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.StreamHandler(sys.stderr)

    if config.format == "json":
        handler.setFormatter(_JsonFormatter())
    elif config.format == "simple":
        handler.setFormatter(_RedactingFormatter(_SIMPLE_FORMAT))
    else:
        handler.setFormatter(_RedactingFormatter(_DETAILED_FORMAT))

    return handler


def _apply(logging_config: LoggingConfig, *, base_path: Path | None = None) -> None:
    """Apply a LoggingConfig to the live Python logging system."""
    effective_level = logging_config.level or _PRESET_LEVELS[logging_config.preset]
    handler_config = logging_config.handler or _PRESET_HANDLER_DEFAULTS[logging_config.preset]

    handler: logging.Handler | None = None
    if handler_config is not None:
        handler = _make_handler(handler_config, base_path=base_path)

    stale_handlers: list[logging.Handler] = []
    for namespace in STACK_LOG_NAMESPACES:
        ns_logger = logging.getLogger(namespace)
        ns_logger.setLevel(effective_level)
        stale_handlers.extend(
            h for h in ns_logger.handlers if not isinstance(h, logging.NullHandler)
        )
        ns_logger.handlers = [
            h for h in ns_logger.handlers if isinstance(h, logging.NullHandler)
        ]
        if handler is not None:
            # This currently configures the same handler on each namespace
            # directly. A future redesign may prefer a shared parent logger,
            # but the current approach keeps adoption simple and explicit.
            ns_logger.addHandler(handler)
            ns_logger.propagate = False
        else:
            # Library mode: propagate to the root so the application's own
            # handlers see stack logs at the configured level.
            ns_logger.propagate = True

    seen_handler_ids: set[int] = set()
    for stale in stale_handlers:
        stale_id = id(stale)
        if stale_id in seen_handler_ids:
            continue
        seen_handler_ids.add(stale_id)
        stale.close()

    for name, level in logging_config.loggers.items():
        logging.getLogger(name).setLevel(level)


def configure_logging(
    config: "LoggingConfig | _HasLoggingConfig | None" = None,
    *,
    preset: Literal["library", "notebook", "application", "production"] | None = None,
) -> None:
    """Configure Python logging for the OMOP stack.

    Safe to call multiple times; re-applying the same config is idempotent.

    Parameters
    ----------
    config:
        A ``LoggingConfig``, a ``StackConfig`` (the ``logging`` block is
        extracted automatically), or ``None``. Mutually exclusive with
        ``preset``.
    preset:
        Shorthand for ``configure_logging(LoggingConfig(preset=...))``.
        Mutually exclusive with ``config``.

    Examples
    --------
    Notebook quick-start — no config file needed::

        configure_logging(preset="notebook")

    From a loaded config file::

        configure_logging(load_stack_config())

    Inline with level override::

        configure_logging(LoggingConfig(preset="application", level="DEBUG"))
    """
    if config is not None and preset is not None:
        raise TypeError("provide config or preset, not both")

    base_path: Path | None = None

    if config is None:
        logging_config: LoggingConfig = LoggingConfig(preset=preset or "library")
    elif isinstance(config, LoggingConfig):
        logging_config = config
    else:
        # Duck-type as StackConfig to avoid a circular import.
        if not hasattr(config, "logging"):
            raise TypeError(
                f"configure_logging() expects a LoggingConfig or StackConfig, got {type(config).__name__!r}"
            )
        file_path: Path | None = getattr(config, "config_file_path", None)
        if file_path is not None:
            base_path = file_path.parent
        logging_config = config.logging

    _apply(logging_config, base_path=base_path)
