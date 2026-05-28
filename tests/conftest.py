"""Shared fixtures for `oa-configurator` tests.

Rule: no test reads from or writes to ~/.config/omop/.
All tests use StackConfig.for_session() or tmp_path.
"""

from __future__ import annotations

import pytest

from oa_configurator import StackConfig


@pytest.fixture
def minimal_stack() -> StackConfig:
    """Minimal in-memory config with one SQLite connection and one resource."""
    return StackConfig.for_session(
        connections={
            "db": {
                "dialect": "sqlite",
                "database": ":memory:",
            }
        },
        resources={
            "default": {
                "primary_db": "db",
                "cdm_schema": "omop",
            }
        },
    )


@pytest.fixture
def pg_stack() -> StackConfig:
    """In-memory config simulating a PostgreSQL CDM setup."""
    return StackConfig.for_session(
        connections={
            "cdm": {
                "dialect": "postgresql+psycopg",
                "host": "localhost",
                "port": 5432,
                "user": "omop",
                "password": "secret",
                "database": "omop_cdm",
            }
        },
        resources={
            "default": {
                "primary_db": "cdm",
                "cdm_schema": "omop",
                "vocab_schema": "omop_vocab",
                "results_schema": "results",
            }
        },
    )
