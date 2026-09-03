"""Phase 9: `omop-config verify`'s schema-provenance section.

Live-Postgres regression: verify() opens guard_schema_provenance() with an
empty body per resolved database entry -- the same agree/disagree check the
DDL-time gate uses, unconditional (no --deep-style gate), refreshing
last_verified_at on success as a side effect.

verify() always builds its own fresh, real engines internally (never the
rollback-protected pg_db.connection), so every provenance row it writes is
a genuine commit. No cleanup here yet -- pending a generic test-cleanup
interface in oa-configurator's testing module (plan Phase 10).
"""

from __future__ import annotations

import uuid

import pytest
from oa_configurator import CDMDatabaseConfig, ConnectionConfig, Role, StackConfig
from oa_configurator.domains.resources.sql import record_schema_provenance
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from oa_configurator.cli import app
from oa_configurator.resolver import Resolver

pytestmark = [pytest.mark.postgresql, pytest.mark.db_dialect]

runner = CliRunner()


def _stack_with_one_cdm_db(pg_db, *, database_name: str, schema: str) -> StackConfig:
    # deliberate test_only=False to ensure the test exercises the real Postgres connection, 
    # not the rollback-protected pg_db.connection.
    url = make_url(pg_db.connection.engine.url)
    return StackConfig.for_session(
        connections={
            "verify_conn": ConnectionConfig(
                dialect=url.drivername, host=url.host, port=url.port,
                user=url.username, password=url.password, database_name=url.database,
                test_only=False,
            )
        },
        databases={
            database_name: CDMDatabaseConfig(connection="verify_conn", schema_name=schema),
        },
    )


def test_verify_reports_ok_for_a_fresh_database(pg_db, monkeypatch):
    db_name = f"verify_db_{uuid.uuid4().hex[:8]}"
    schema = f"test_{uuid.uuid4().hex[:8]}"
    stack = _stack_with_one_cdm_db(pg_db, database_name=db_name, schema=schema)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack)

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert db_name in result.output
    assert "DRIFT" not in result.output
    assert "FAIL" not in result.output


def test_verify_reports_drift_after_reconfiguring_the_schema(pg_db, monkeypatch):
    db_name = f"verify_db_{uuid.uuid4().hex[:8]}"
    schema_a = f"test_{uuid.uuid4().hex[:8]}"
    schema_b = f"test_{uuid.uuid4().hex[:8]}"

    stack_a = _stack_with_one_cdm_db(pg_db, database_name=db_name, schema=schema_a)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack_a)
    first = runner.invoke(app, ["verify"])
    assert first.exit_code == 0, first.output

    stack_b = _stack_with_one_cdm_db(pg_db, database_name=db_name, schema=schema_b)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack_b)
    second = runner.invoke(app, ["verify"])
    assert second.exit_code == 1
    assert "DRIFT" in second.output


def test_verify_clean_after_acknowledging_drift(pg_db, monkeypatch):
    db_name = f"verify_db_{uuid.uuid4().hex[:8]}"
    schema_a = f"test_{uuid.uuid4().hex[:8]}"
    schema_b = f"test_{uuid.uuid4().hex[:8]}"

    stack_a = _stack_with_one_cdm_db(pg_db, database_name=db_name, schema=schema_a)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack_a)
    runner.invoke(app, ["verify"])

    # vocab/results fall back to schema_name (unconfigured), so verify()
    # checks all three roles for a CDM database -- all three drifted.
    stack_b = _stack_with_one_cdm_db(pg_db, database_name=db_name, schema=schema_b)
    resolved = Resolver(stack_b).resolve_database(db_name)
    with pg_db.connection.engine.begin() as connection:
        for role in (Role.PRIMARY, Role.VOCAB, Role.RESULTS):
            record_schema_provenance(
                connection, resolved, role=role, new_schema=schema_b, reason="test acknowledgment"
            )

    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack_b)
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "DRIFT" not in result.output
