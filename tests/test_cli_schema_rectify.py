"""`omop-config acknowledge-schema-migration` / `drop-orphan-schema-tables`.

Live-Postgres regression for the two CLI commands ported from OMOP_Alchemy's
maintenance CLI (Phase 3.2.D): generic over any [databases.*] entry, not
CDM-specific. Both build their own fresh, real engines internally (never the
rollback-protected pg_db.connection), so every write is a genuine commit --
cleanup_after_test cleans up rows/tables each test leaves behind.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from oa_configurator import CDMDatabaseConfig, ConnectionConfig, StackConfig
from oa_configurator.domains.resources.sql import SCHEMA_PROVENANCE_SCHEMA, _schema_provenance_table
from oa_configurator.testing import delete_rows_on_cleanup
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from oa_configurator.cli import app

pytestmark = [pytest.mark.postgresql, pytest.mark.db_dialect]

runner = CliRunner()


def _stack_with_one_cdm_db(pg_db, *, database_name: str, schema: str) -> StackConfig:
    # deliberate test_only=False: these commands build their own real engine,
    # never the rollback-protected pg_db.connection.
    url = make_url(pg_db.connection.engine.url)
    return StackConfig.for_session(
        connections={
            "rectify_conn": ConnectionConfig(
                dialect=url.drivername, host=url.host, port=url.port,
                user=url.username, password=url.password, database_name=url.database,
                test_only=False,
            )
        },
        databases={
            database_name: CDMDatabaseConfig(connection="rectify_conn", cdm_schema=schema),
        },
    )


def _cleanup_provenance_rows(cleanup_after_test, pg_db, db_name: str) -> None:
    table = _schema_provenance_table(SCHEMA_PROVENANCE_SCHEMA)
    delete_rows_on_cleanup(
        cleanup_after_test, pg_db.connection.engine, table, table.c.database_name == db_name
    )


def test_acknowledge_schema_migration_clears_drift(pg_db, monkeypatch, cleanup_after_test):
    db_name = f"rectify_db_{uuid.uuid4().hex[:8]}"
    schema_a = f"test_{uuid.uuid4().hex[:8]}"
    schema_b = f"test_{uuid.uuid4().hex[:8]}"
    _cleanup_provenance_rows(cleanup_after_test, pg_db, db_name)

    stack_a = _stack_with_one_cdm_db(pg_db, database_name=db_name, schema=schema_a)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack_a)
    assert runner.invoke(app, ["verify"]).exit_code == 0

    stack_b = _stack_with_one_cdm_db(pg_db, database_name=db_name, schema=schema_b)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack_b)
    assert runner.invoke(app, ["verify"]).exit_code == 1

    for role in ("primary", "vocab", "results"):
        result = runner.invoke(
            app,
            [
                "acknowledge-schema-migration",
                "--database", db_name,
                "--schema-tag", role,
                "--new-schema", schema_b,
                "--reason", "test acknowledgment",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Acknowledged" in result.output

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "DRIFT" not in result.output


def test_acknowledge_schema_migration_requires_a_reason(pg_db, monkeypatch, cleanup_after_test):
    db_name = f"rectify_db_{uuid.uuid4().hex[:8]}"
    schema = f"test_{uuid.uuid4().hex[:8]}"
    _cleanup_provenance_rows(cleanup_after_test, pg_db, db_name)
    stack = _stack_with_one_cdm_db(pg_db, database_name=db_name, schema=schema)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack)

    result = runner.invoke(
        app, ["acknowledge-schema-migration", "--database", db_name, "--new-schema", schema]
    )
    assert result.exit_code != 0


def test_drop_orphan_schema_tables_previews_then_drops(pg_db, monkeypatch, cleanup_after_test):
    db_name = f"rectify_db_{uuid.uuid4().hex[:8]}"
    cdm_schema = f"test_{uuid.uuid4().hex[:8]}"
    orphan_schema = f"orphan_{uuid.uuid4().hex[:8]}"
    stack = _stack_with_one_cdm_db(pg_db, database_name=db_name, schema=cdm_schema)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack)

    engine = pg_db.connection.engine
    with engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{orphan_schema}"'))
        connection.execute(sa.text(f'CREATE TABLE "{orphan_schema}".leftover (id integer)'))
        connection.execute(sa.text(f'INSERT INTO "{orphan_schema}".leftover VALUES (1), (2)'))

    def _drop_schema():
        with engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{orphan_schema}" CASCADE'))

    cleanup_after_test(_drop_schema)

    preview = runner.invoke(
        app, ["drop-orphan-schema-tables", "--database", db_name, "--schema", orphan_schema]
    )
    assert preview.exit_code == 0, preview.output
    assert "Would drop" in preview.output
    assert "leftover" in preview.output
    assert "2 rows" in preview.output

    with engine.connect() as connection:
        assert connection.execute(
            sa.text(f'SELECT COUNT(*) FROM "{orphan_schema}".leftover')
        ).scalar() == 2

    confirmed = runner.invoke(
        app,
        ["drop-orphan-schema-tables", "--database", db_name, "--schema", orphan_schema, "--confirm"],
    )
    assert confirmed.exit_code == 0, confirmed.output
    assert "Dropped" in confirmed.output

    with engine.connect() as connection:
        exists = connection.execute(
            sa.text("SELECT to_regclass(:name)"), {"name": f"{orphan_schema}.leftover"}
        ).scalar()
    assert exists is None


def test_drop_orphan_schema_tables_refuses_a_schema_still_in_use(pg_db, monkeypatch, cleanup_after_test):
    db_name = f"rectify_db_{uuid.uuid4().hex[:8]}"
    cdm_schema = f"test_{uuid.uuid4().hex[:8]}"
    stack = _stack_with_one_cdm_db(pg_db, database_name=db_name, schema=cdm_schema)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack)

    engine = pg_db.connection.engine
    with engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{cdm_schema}"'))

    def _drop_schema():
        with engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{cdm_schema}" CASCADE'))

    cleanup_after_test(_drop_schema)

    result = runner.invoke(
        app, ["drop-orphan-schema-tables", "--database", db_name, "--schema", cdm_schema]
    )
    assert result.exit_code == 1
    assert "Refusing to drop" in result.output
