"""Shared fixtures for `oa-configurator` tests.

Rule: no test reads from or writes to ~/.config/omop/.
All tests use StackConfig.for_session() or tmp_path.
"""

from __future__ import annotations

import pytest

from oa_configurator import (
    FS_CDM_SCHEMA,
    FS_DATABASE,
    FS_RESULTS_SCHEMA,
    FS_VOCAB_SCHEMA,
    StackConfig,
    DatabaseConfig,
)


@pytest.fixture
def minimal_stack() -> StackConfig:
    """Minimal in-memory config with one SQLite connection and one resource."""
    return StackConfig.for_session(
        databases={
            "db": DatabaseConfig(
                dialect="sqlite",
                database_name=":memory:",
            )
        },
        resources={
            "default": {
                FS_DATABASE.name: "db",
                FS_CDM_SCHEMA.name: "omop",
            }
        },
    )


@pytest.fixture
def pg_stack() -> StackConfig:
    """In-memory config simulating a PostgreSQL CDM setup."""
    return StackConfig.for_session(
        databases={
            "cdm": DatabaseConfig(
                dialect="postgresql+psycopg",
                host="localhost",
                port=5432,
                user="omop",
                password="secret",
                database_name="omop_cdm",
            )
        },
        resources={
            "default": {
                FS_DATABASE.name: "cdm",
                FS_CDM_SCHEMA.name: "omop",
                FS_VOCAB_SCHEMA.name: "omop_vocab",
                FS_RESULTS_SCHEMA.name: "results",
            }
        },
    )
