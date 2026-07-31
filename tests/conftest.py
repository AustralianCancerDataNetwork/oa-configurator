"""Shared fixtures for `oa-configurator` tests.

Rule: no test reads from or writes to ~/.config/omop/.
All tests use StackConfig.for_session() or tmp_path.
"""

from __future__ import annotations

import pytest

from oa_configurator import (
    StackConfig,
    ConnectionConfig,
    DatabaseConfig,
)


@pytest.fixture
def minimal_stack() -> StackConfig:
    """Minimal in-memory config with one SQLite connection and one database."""
    return StackConfig.for_session(
        connections={
            "db": ConnectionConfig(
                dialect="sqlite",
                database_name=":memory:",
            )
        },
        databases={
            "default": DatabaseConfig(connection="db", cdm_schema="omop"),
        },
    )


@pytest.fixture
def pg_stack() -> StackConfig:
    """In-memory config simulating a PostgreSQL CDM setup."""
    return StackConfig.for_session(
        connections={
            "cdm": ConnectionConfig(
                dialect="postgresql+psycopg",
                host="localhost",
                port=5432,
                user="omop",
                password="secret",
                database_name="omop_cdm",
            )
        },
        databases={
            "default": DatabaseConfig(
                connection="cdm",
                cdm_schema="omop",
                vocab_schema="omop_vocab",
                results_schema="results",
            ),
        },
    )
