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
from oa_configurator.logging_config import RedactingFilter
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


DSN = "postgresql://user:pw@host/db"


def _emit_through(console, message, args=None):
    """Log *message* through a real configure_logging() handler and capture output.

    Goes through ``configure_logging`` rather than a hand-built handler, because
    which handler gets the redaction is exactly what regressed: the guarantee held
    on the plain path and vanished when a console was passed.
    """
    import io

    buffer = io.StringIO()
    if console:
        from rich.console import Console

        configure_logging(verbosity=2, extra_namespaces=["probe"],
                          console=Console(file=buffer, width=200, no_color=True, markup=False))
    else:
        configure_logging(verbosity=2, extra_namespaces=["probe"])
        handler = logging.getLogger("probe").handlers[0]
        handler.stream = buffer
    logging.getLogger("probe").warning(message, *(args or ()))
    return buffer.getvalue()


class TestUrlRedactionIsHandlerIndependent:
    """The same guarantee whichever handler is configured.

    ``RichHandler`` renders the record itself, so the redaction cannot live in a
    formatter: it held on ``StreamHandler`` and was bypassed entirely the moment a
    caller passed ``console=``.
    """

    def setup_method(self):
        _reset("probe")

    def teardown_method(self):
        _reset("probe")

    @pytest.mark.parametrize("console", [False, True], ids=["stream", "rich"])
    def test_dsn_password_is_masked(self, console):
        output = _emit_through(console, "connecting to %s", (DSN,))
        assert "user:pw@" not in output
        assert "user:***@host/db" in output

    @pytest.mark.parametrize("console", [False, True], ids=["stream", "rich"])
    def test_query_values_are_masked(self, console):
        output = _emit_through(console, "GET https://host/v1?api_key=abc&model=gpt")
        assert "abc" not in output
        assert "api_key=***" in output and "model=***" in output

    @pytest.mark.parametrize("console", [False, True], ids=["stream", "rich"])
    def test_ordinary_text_is_untouched(self, console):
        output = _emit_through(console, "connecting to the vocabulary database")
        assert "connecting to the vocabulary database" in output

    @pytest.mark.parametrize("console", [False, True], ids=["stream", "rich"])
    def test_a_deliberately_extracted_secret_is_not_caught(self, console):
        """Both paths, so nobody closes this by adding a key-name rule to one."""
        connection = ConnectionConfig(
            dialect="postgresql+psycopg", host="h", user="u",
            password="hunter2", database_name="omop",
        )
        output = _emit_through(console, "password=%s", (connection.password,))
        assert "password=hunter2" in output

    @pytest.mark.parametrize("console", [False, True], ids=["stream", "rich"])
    def test_a_logged_config_object_is_safe_without_the_scrubber(self, console):
        """Safety here comes from SecretSafeModel, not from this filter."""
        connection = ConnectionConfig(
            dialect="postgresql+psycopg", host="h", user="u",
            password="pw-CANARY", database_name="omop",
        )
        assert "pw-CANARY" not in _emit_through(console, "%s", (connection,))
        assert "pw-CANARY" not in _emit_through(console, "%r", (connection,))


class TestRedactingFilter:
    def test_it_is_installed_on_both_handler_types(self):
        """Pins the mechanism, not just the outcome."""
        _reset("probe")
        configure_logging(verbosity=2, extra_namespaces=["probe"])
        plain = logging.getLogger("probe").handlers[0]
        assert any(isinstance(f, RedactingFilter) for f in plain.filters)

        from rich.console import Console
        _reset("probe")
        configure_logging(verbosity=2, extra_namespaces=["probe"], console=Console())
        rich_handler = logging.getLogger("probe").handlers[0]
        assert any(isinstance(f, RedactingFilter) for f in rich_handler.filters)
        _reset("probe")

    def test_records_without_a_url_keep_their_lazy_args(self):
        """Only a record that actually carried a URL is rewritten, so structured
        handlers downstream still see the original fields."""
        record = logging.LogRecord("t", logging.WARNING, "", 0, "count=%d", (3,), None)
        RedactingFilter().filter(record)
        assert record.args == (3,)
        assert record.msg == "count=%d"

    def test_a_record_carrying_a_url_is_rewritten_once(self):
        record = logging.LogRecord("t", logging.WARNING, "", 0, "at %s", (DSN,), None)
        f = RedactingFilter()
        f.filter(record)
        assert record.args is None
        assert record.msg == "at postgresql://user:***@host/db"
        f.filter(record)  # idempotent: a second handler with the same filter
        assert record.msg == "at postgresql://user:***@host/db"

    def test_the_traceback_is_scrubbed_too(self):
        """Formatter.format() appends exc_text verbatim once it is set."""
        try:
            raise ValueError(f"could not connect: {DSN}")
        except ValueError:
            record = logging.LogRecord("t", logging.ERROR, "", 0, "failed", None,
                                       __import__("sys").exc_info())
        RedactingFilter().filter(record)
        assert "user:pw@" not in record.exc_text
        assert "user:***@host/db" in record.exc_text


class TestRedactingFormatterStillWorks:
    """Public API, retained for callers who wired it up directly."""

    def test_it_shares_the_filter_s_scrubbing(self):
        assert _formatted(DSN) == "postgresql://user:***@host/db"
