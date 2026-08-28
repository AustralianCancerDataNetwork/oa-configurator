"""Logging configuration for the OMOP stack."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, Protocol

from pydantic import ConfigDict, Field, field_validator
from typing_extensions import deprecated

from .refs import MASK, SecretSafeBaseModel, safe_endpoint

_OWN_NAMESPACE = "oa_configurator"

_FORMAT = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_VERBOSITY_LEVELS = {0: "WARNING", 1: "INFO", 2: "DEBUG"}

_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
"""Matches a bare URL anywhere in a message.
"""


def _scrub_urls(text: str) -> str:
    """Mask credentials in every URL in *text*, via the shared primitive.

    The single implementation of what a safe URL looks like: both
    :class:`RedactingFilter` and :class:`RedactingFormatter` route through here,
    and both delegate the actual masking to
    :func:`~oa_configurator.refs.safe_endpoint`.
    """
    return _URL_RE.sub(lambda m: safe_endpoint(m.group(0)) or MASK, text)


class RedactingFilter(logging.Filter):
    """Scrubs credential-bearing URLs before a handler emits its record.

    A filter rather than a formatter, for two reasons.

    **It reaches handlers that do their own rendering.** e.g. ``RichHandler`` -
    a formatter attached to it governs only part of the result; 
    Handler filters run in ``Handler.handle()`` before ``emit()``, so concole
    and stream paths are covered by one mechanism and neither needs its own code 
    path.

    **It survives customisation.** Replacing a handler's formatter is an ordinary
    thing to do and must not silently remove a security guarantee. Redaction is
    not a presentation concern, so it does not live in the presentation layer.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        scrubbed = _scrub_urls(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = None
        if record.exc_info and not record.exc_text:
            # Caching a scrubbed traceback here is what keeps the exception path
            # covered: Formatter.format() appends record.exc_text verbatim when it
            # is already set, rather than re-deriving it from exc_info.
            record.exc_text = _scrub_urls(
                logging.Formatter().formatException(record.exc_info)
            )
        return True


@deprecated(
    "RedactingFormatter is deprecated; configure_logging installs RedactingFilter "
    "instead, which also covers handlers (e.g. RichHandler) that render the record "
    "themselves. Use RedactingFilter directly."
)
class RedactingFormatter(logging.Formatter):
    """Deprecated: formatter applying the same URL scrubbing as :class:`RedactingFilter`."""

    def format(self, record: logging.LogRecord) -> str:
        return _scrub_urls(super().format(record))


def _coerce_level(value: str) -> str:
    upper = value.strip().upper()
    if not hasattr(logging, upper):
        raise ValueError(
            f"Unknown log level: {value!r}. Use DEBUG, INFO, WARNING, ERROR, or CRITICAL."
        )
    return upper


class LoggingConfig(SecretSafeBaseModel):
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
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler.addFilter(RedactingFilter())

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
