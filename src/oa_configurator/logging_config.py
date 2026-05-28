"""Logging configuration for the OMOP stack."""

from __future__ import annotations

import logging
import sys
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

_OWN_NAMESPACE = "oa_configurator"

_FORMAT = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_VERBOSITY_LEVELS = {0: "WARNING", 1: "INFO", 2: "DEBUG"}


def _coerce_level(value: str) -> str:
    upper = value.strip().upper()
    if not hasattr(logging, upper):
        raise ValueError(
            f"Unknown log level: {value!r}. Use DEBUG, INFO, WARNING, ERROR, or CRITICAL."
        )
    return upper


class LoggingConfig(BaseModel):
    """Logging overrides from the ``[logging]`` section of config.toml.

    ``level`` overrides the verbosity-derived level for all OMOP loggers.
    ``loggers`` applies fine-grained level overrides to specific named loggers
    (e.g. ``{"sqlalchemy.engine": "INFO"}``).
    """

    model_config = ConfigDict(extra="forbid")

    level: str | None = Field(
        default=None,
        description="Override level for all OMOP loggers. Supersedes CLI verbosity.",
    )
    loggers: dict[str, str] = Field(
        default_factory=dict,
        description="Fine-grained level overrides keyed by logger name.",
    )

    @field_validator("level")
    @classmethod
    def _validate_level(cls, v: str | None) -> str | None:
        return None if v is None else _coerce_level(v)

    @field_validator("loggers")
    @classmethod
    def _validate_loggers(cls, v: dict[str, str]) -> dict[str, str]:
        return {name: _coerce_level(level) for name, level in v.items()}


class _HasLoggingConfig(Protocol):
    logging: LoggingConfig


def configure_logging(
    config: "LoggingConfig | _HasLoggingConfig | None" = None,
    *,
    verbosity: int = 0,
    extra_namespaces: list[str] | None = None,
) -> None:
    """Configure Python logging for the OMOP stack.

    Safe to call multiple times; re-applying the same arguments is idempotent.

    Parameters
    ----------
    config:
        A ``LoggingConfig`` or a ``StackConfig`` (``logging`` block extracted
        automatically). When ``config.level`` is set it takes precedence over
        ``verbosity``.
    verbosity:
        Number of ``-v`` flags passed by the user. ``0`` → WARNING,
        ``1`` → INFO, ``2+`` → DEBUG.
    extra_namespaces:
        Additional logger namespaces to configure alongside ``oa_configurator``.
        Pass your package name here, e.g. ``extra_namespaces=["omop_graph"]``.
    """
    logging_config: LoggingConfig
    if config is None:
        logging_config = LoggingConfig()
    elif isinstance(config, LoggingConfig):
        logging_config = config
    else:
        if not hasattr(config, "logging"):
            raise TypeError(
                f"configure_logging() expects a LoggingConfig or StackConfig, "
                f"got {type(config).__name__!r}"
            )
        logging_config = config.logging

    effective_level = logging_config.level or _VERBOSITY_LEVELS.get(verbosity, "INFO")

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    namespaces = (_OWN_NAMESPACE,) + tuple(extra_namespaces or [])
    stale: list[logging.Handler] = []
    for ns in namespaces:
        ns_logger = logging.getLogger(ns)
        ns_logger.setLevel(effective_level)
        stale.extend(h for h in ns_logger.handlers if not isinstance(h, logging.NullHandler))
        ns_logger.handlers = [h for h in ns_logger.handlers if isinstance(h, logging.NullHandler)]
        ns_logger.addHandler(handler)
        ns_logger.propagate = False

    seen: set[int] = set()
    for h in stale:
        if id(h) not in seen:
            seen.add(id(h))
            h.close()

    for name, level in logging_config.loggers.items():
        logging.getLogger(name).setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger — single import point for consuming packages."""
    return logging.getLogger(name)
