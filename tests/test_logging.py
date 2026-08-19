"""Tests for logging_config.py: configure_logging, verbosity, LoggingConfig."""

from __future__ import annotations

import logging

import pytest

from oa_configurator import (
    ConnectionConfig,
    RedactingFormatter,
    configure_logging,
    get_logger,
)
from oa_configurator.logging_config import LoggingConfig


def _reset(*names: str) -> None:
    for name in ("oa_configurator", *names):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        lg.level = logging.NOTSET


class TestConfigureLogging:
    def setup_method(self):
        _reset("test_ns")

    def test_verbosity_0_sets_warning(self):
        configure_logging(verbosity=0)
        assert logging.getLogger("oa_configurator").level == logging.WARNING

    def test_verbosity_1_sets_info(self):
        configure_logging(verbosity=1)
        assert logging.getLogger("oa_configurator").level == logging.INFO

    def test_verbosity_2_sets_debug(self):
        configure_logging(verbosity=2)
        assert logging.getLogger("oa_configurator").level == logging.DEBUG

    def test_config_level_overrides_verbosity(self):
        cfg = LoggingConfig(level="DEBUG")
        configure_logging(cfg, verbosity=0)
        assert logging.getLogger("oa_configurator").level == logging.DEBUG

    def test_extra_namespaces_configured(self):
        configure_logging(verbosity=1, extra_namespaces=["test_ns"])
        assert logging.getLogger("test_ns").level == logging.INFO

    def test_extra_namespaces_none_no_error(self):
        configure_logging(verbosity=0, extra_namespaces=None)

    def test_idempotent_reconfigure(self):
        configure_logging(verbosity=1)
        configure_logging(verbosity=1)
        assert logging.getLogger("oa_configurator").level == logging.INFO

    def test_stack_config_duck_type(self):
        class FakeStack:
            logging = LoggingConfig(level="INFO")

        configure_logging(FakeStack())
        assert logging.getLogger("oa_configurator").level == logging.INFO

    def test_invalid_config_type_raises(self):
        with pytest.raises(TypeError):
            configure_logging("not a config")  # type: ignore


class TestGetLogger:
    def test_returns_logger(self):
        lg = get_logger("oa_configurator.test")
        assert isinstance(lg, logging.Logger)
        assert lg.name == "oa_configurator.test"


class TestLoggingConfig:
    def test_defaults(self):
        cfg = LoggingConfig()
        assert cfg.level is None
        assert cfg.loggers == {}

    def test_level_normalised_to_upper(self):
        cfg = LoggingConfig(level="info")
        assert cfg.level == "INFO"

    def test_invalid_level_raises(self):
        with pytest.raises(Exception):
            LoggingConfig(level="SUPERVERBOSE")

    def test_loggers_validated(self):
        cfg = LoggingConfig(loggers={"sqlalchemy.engine": "debug"})
        assert cfg.loggers["sqlalchemy.engine"] == "DEBUG"


def _formatted(message: str) -> str:
    """Run *message* through the formatter exactly as a handler would."""
    record = logging.LogRecord("t", logging.WARNING, "", 0, message, None, None)
    return RedactingFormatter("%(message)s").format(record)


class TestRedactingFormatterScope:
    """The formatter no longer guesses which key names are sensitive.

    Config objects are safe to render on their own account (see
    ``SecretSafeModel``), so the key-name word list had nothing left to protect
    and was actively masking non-secrets like ``base_url`` for ending in ``url``.
    """

    def test_a_deliberately_extracted_secret_is_not_caught(self):
        """Naming the field and choosing the sink is the caller's decision.

        Pinned so nobody restores the word list on the strength of this line: the
        contract is that oa-configurator protects config *objects*, not values a
        caller has gone and fetched.
        """
        assert _formatted("password=hunter2") == "password=hunter2"

    def test_ordinary_words_are_never_touched(self):
        for message in ("monkey=x", "turkey=1", "donkey_count=3", "api_version=2024-02-01"):
            assert _formatted(message) == message

    def test_a_rendered_config_is_safe_without_the_formatter(self):
        """The formatter is not what protects a logged config object."""
        connection = ConnectionConfig(
            dialect="postgresql+psycopg", host="h", user="u",
            password="pw-CANARY", database_name="omop",
        )
        record = logging.LogRecord("t", logging.WARNING, "", 0, "%s", (connection,), None)
        assert "pw-CANARY" not in logging.Formatter("%(message)s").format(record)
        assert "pw-CANARY" not in _formatted(f"connecting with {connection}")


class TestRedactingFormatterUrls:
    """A URL carries its credential with no ``key=`` to anchor on.

    Routed through ``safe_endpoint`` rather than a second redaction rule, so
    there is one audited answer to what a safe URL looks like.
    """

    def test_bare_url_password_is_masked(self):
        assert _formatted("postgresql://user:pw@host/db") == (
            "postgresql://user:***@host/db"
        )

    def test_host_and_path_survive_for_diagnosis(self):
        formatted = _formatted("could not connect to postgresql://u:pw@db.example/omop")
        assert "db.example" in formatted
        assert "/omop" in formatted
        assert "pw" not in formatted

    def test_query_values_in_a_logged_url_are_masked(self):
        formatted = _formatted("GET https://api.example.org/v1?api_key=sk-x failed")
        assert "sk-x" not in formatted
        assert "api_key" in formatted

    def test_url_in_an_assignment_is_masked_as_a_url(self):
        """The key name is irrelevant now; the URL shape is what is recognised."""
        assert _formatted("url=postgres://u:pw@h/db") == "url=postgres://u:***@h/db"

    def test_non_url_text_is_untouched(self):
        assert _formatted("connecting to the database") == "connecting to the database"
