"""Tests for logging_config.py — configure_logging, presets, RedactingFormatter."""

from __future__ import annotations

import logging

import pytest

from oa_configurator import configure_logging, get_logger
from oa_configurator.logging_config import LoggingConfig, RedactingFormatter


def _reset_logging():
    """Remove handlers from oa_configurator namespace between tests."""
    for name in ("oa_configurator", "test_ns"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        lg.level = logging.NOTSET


class TestConfigureLogging:
    def setup_method(self):
        _reset_logging()

    def test_library_preset_sets_warning(self):
        configure_logging(preset="library")
        lg = logging.getLogger("oa_configurator")
        assert lg.level == logging.WARNING

    def test_application_preset_sets_info(self):
        configure_logging(preset="application")
        lg = logging.getLogger("oa_configurator")
        assert lg.level == logging.INFO

    def test_extra_namespaces_configured(self):
        configure_logging(preset="application", extra_namespaces=["test_ns"])
        lg = logging.getLogger("test_ns")
        assert lg.level == logging.INFO

    def test_extra_namespaces_none_no_error(self):
        configure_logging(preset="application", extra_namespaces=None)

    def test_idempotent_reconfigure(self):
        configure_logging(preset="application")
        configure_logging(preset="application")
        lg = logging.getLogger("oa_configurator")
        assert lg.level == logging.INFO

    def test_config_object(self):
        cfg = LoggingConfig(preset="notebook")
        configure_logging(cfg)
        lg = logging.getLogger("oa_configurator")
        assert lg.level == logging.INFO

    def test_stack_config_duck_type(self):
        from oa_configurator import StackConfig
        from oa_configurator.logging_config import LoggingConfig

        class FakeStack:
            logging = LoggingConfig(preset="application")

        configure_logging(FakeStack())
        lg = logging.getLogger("oa_configurator")
        assert lg.level == logging.INFO


class TestGetLogger:
    def test_returns_logger(self):
        lg = get_logger("oa_configurator.test")
        assert isinstance(lg, logging.Logger)
        assert lg.name == "oa_configurator.test"


class TestRedactingFormatter:
    def test_redacts_password_in_url(self):
        fmt = RedactingFormatter("%(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="connecting to postgresql://user:s3cret@host/db",
            args=(),
            exc_info=None,
        )
        output = fmt.format(record)
        assert "s3cret" not in output
        assert "***" in output

    def test_leaves_safe_strings_alone(self):
        fmt = RedactingFormatter("%(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="normal log message with no secrets",
            args=(),
            exc_info=None,
        )
        output = fmt.format(record)
        assert output == "normal log message with no secrets"


class TestLoggingConfig:
    def test_default_preset(self):
        cfg = LoggingConfig()
        assert cfg.preset == "library"

    def test_production_preset(self):
        cfg = LoggingConfig(preset="production")
        assert cfg.preset == "production"

    def test_invalid_preset(self):
        with pytest.raises(Exception):
            LoggingConfig(preset="invalid_preset")
