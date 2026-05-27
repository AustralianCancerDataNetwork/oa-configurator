"""Tests for logging_config.py — configure_logging, verbosity, LoggingConfig."""

from __future__ import annotations

import logging

import pytest

from oa_configurator import configure_logging, get_logger
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
