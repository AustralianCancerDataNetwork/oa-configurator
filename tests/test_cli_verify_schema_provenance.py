"""`omop-config verify`'s schema-provenance section.

Live-Postgres regression. verify() opens the schema-provenance guard with an
empty body per resolved database entry, the same agree/disagree check the
DDL-time gate uses. It registers no claims itself; tests establish the
baseline through create_engine() first.

verify() builds its own fresh, real engines internally (never the
rollback-protected pg_db.connection), so every write is a genuine commit.
fresh_role_registry_rows restores the Role-tag rows around each test.
"""

from __future__ import annotations

import uuid

import pytest
from oa_configurator import CDMDatabaseConfig, ConnectionConfig, Role, StackConfig
from oa_configurator.domains.resources.schema_registry import _record_schema_provenance
from typer.testing import CliRunner

from oa_configurator.cli import app
from oa_configurator.resolver import Resolver

pytestmark = [pytest.mark.postgresql, pytest.mark.db_dialect, pytest.mark.usefixtures("fresh_role_registry_rows")]

runner = CliRunner()


def _stack_with_one_cdm_db(
    connection_config: ConnectionConfig, *, database_config_name: str, schema: str
) -> StackConfig:
    return StackConfig.for_session(
        connections={"verify_conn": connection_config},
        databases={
            database_config_name: CDMDatabaseConfig(connection="verify_conn", cdm_schema=schema),
        },
    )


def _register(stack: StackConfig, database_config_name: str) -> None:
    """Register the database's Role claims, as its first create_engine() does."""
    Resolver(stack).resolve_database(database_config_name).create_engine().dispose()


def test_verify_reports_ok_for_a_registered_database(pg_connection_config, monkeypatch):
    db_name = f"verify_db_{uuid.uuid4().hex[:8]}"
    schema = f"test_{uuid.uuid4().hex[:8]}"
    stack = _stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name, schema=schema)
    _register(stack, db_name)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack)

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert db_name in result.output
    assert "DRIFT" not in result.output
    assert "FAIL" not in result.output


def test_verify_registers_no_baseline(pg_connection_config, monkeypatch):
    db_name = f"verify_db_{uuid.uuid4().hex[:8]}"
    schema = f"test_{uuid.uuid4().hex[:8]}"
    stack = _stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name, schema=schema)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack)

    for _ in range(2):
        result = runner.invoke(app, ["verify"])
        assert result.exit_code == 1
        assert "DRIFT" in result.output


def test_verify_reports_drift_after_reconfiguring_the_schema(pg_connection_config, monkeypatch):
    db_name = f"verify_db_{uuid.uuid4().hex[:8]}"
    schema_a = f"test_{uuid.uuid4().hex[:8]}"
    schema_b = f"test_{uuid.uuid4().hex[:8]}"
    _register(_stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name, schema=schema_a), db_name)

    stack_b = _stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name, schema=schema_b)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack_b)
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1
    assert "DRIFT" in result.output


def test_verify_clean_after_acknowledging_drift(pg_db, pg_connection_config, monkeypatch):
    db_name = f"verify_db_{uuid.uuid4().hex[:8]}"
    schema_a = f"test_{uuid.uuid4().hex[:8]}"
    schema_b = f"test_{uuid.uuid4().hex[:8]}"
    _register(_stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name, schema=schema_a), db_name)

    # vocab/results fall back to schema_name (unconfigured), so verify()
    # checks all three roles for a CDM database, and all three drifted.
    stack_b = _stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name, schema=schema_b)
    resolved = Resolver(stack_b).resolve_database(db_name)
    with pg_db.committing_engine.begin() as connection:
        for role in (Role.PRIMARY, Role.VOCAB, Role.RESULTS):
            _record_schema_provenance(
                connection, database_config_name=resolved.name, schema_tag=role,
                new_physical_schema=schema_b, reason="test acknowledgment",
            )

    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack_b)
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "DRIFT" not in result.output
