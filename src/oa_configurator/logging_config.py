"""Logging configuration for the OMOP stack."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, Protocol

from pydantic import ConfigDict, Field, field_validator

from .refs import MASK, SecretSafeModel, safe_endpoint

_OWN_NAMESPACE = "oa_configurator"

_FORMAT = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_VERBOSITY_LEVELS = {0: "WARNING", 1: "INFO", 2: "DEBUG"}

_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)


class RedactingFormatter(logging.Formatter):
    """Logging formatter that masks credentials in URLs written by other libraries.

    Config objects are safe to log on their own account: every field declared
    ``Sensitive()`` is masked in ``repr`` and ``str`` by
    :class:`~oa_configurator.refs.SecretSafeModel`, so ``logger.info("%s", config)``
    cannot expose one. 

    It does not attempt to catch a caller who extracts a secret deliberately.
    ``logger.info("password=%s", config.password)`` names the field and chooses to
    log it at the caller's decision, which is not an accident this library can 
    prevent without guessing.
    """

    def format(self, record: logging.LogRecord) -> str:
        return _URL_RE.sub(
            lambda m: safe_endpoint(m.group(0)) or MASK, super().format(record)
        )


def _coerce_level(value: str) -> str:
    upper = value.strip().upper()
    if not hasattr(logging, upper):
        raise ValueError(
            f"Unknown log level: {value!r}. Use DEBUG, INFO, WARNING, ERROR, or CRITICAL."
        )
    return upper


class LoggingConfig(SecretSafeModel):
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
    config: LoggingConfig | _HasLoggingConfig | None = None,
    *,
    verbosity: int = 0,
    extra_namespaces: list[str] | None = None,
    console: Any = None,
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

    if console is not None:
        from rich.logging import RichHandler
        handler: logging.Handler = RichHandler(console=console, show_path=False, rich_tracebacks=True)
    else:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(RedactingFormatter(_FORMAT, datefmt=_DATEFMT))

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
    """Return a named logger; single import point for consuming packages."""
    return logging.getLogger(name)
