"""`omop-config acknowledge-schema-migration` / `drop-orphan-schema-tables`.

Live-Postgres regression for the two CLI commands ported from OMOP_Alchemy's
maintenance CLI (Phase 3.2.D): generic over any [databases.*] entry, not
CDM-specific. Both build their own fresh, real engines internally (never the
rollback-protected pg_db.connection), so every write is a genuine commit.
cleanup_after_test cleans up rows/tables each test leaves behind.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from oa_configurator import CDMDatabaseConfig, ConnectionConfig, Resolver, StackConfig
from oa_configurator.domains.resources.schema_registry import _record_schema_provenance
from oa_configurator.domains.resources.sql import connection_key
from oa_configurator.testing import reset_schema_registry_rows
from typer.testing import CliRunner

from oa_configurator.cli import app

pytestmark = [pytest.mark.postgresql, pytest.mark.db_dialect, pytest.mark.usefixtures("fresh_role_registry_rows")]

runner = CliRunner()


def _stack_with_one_cdm_db(
    connection_config: ConnectionConfig, *, database_config_name: str, schema: str
) -> StackConfig:
    return StackConfig.for_session(
        connections={"rectify_conn": connection_config},
        databases={
            database_config_name: CDMDatabaseConfig(connection="rectify_conn", cdm_schema=schema),
        },
    )


def _register(stack: StackConfig, database_config_name: str) -> None:
    """Register the database's Role claims, as its first create_engine() does."""
    Resolver(stack).resolve_database(database_config_name).create_engine().dispose()


def _registry_row(pg_db, schema_tag: str) -> sa.Row:
    registry = sa.Table(
        "schema_registry", sa.MetaData(), autoload_with=pg_db.connection, schema="oa_configurator_provenance"
    )
    with pg_db.committing_engine.connect() as connection:
        return connection.execute(
            sa.select(registry).where(
                registry.c.connection_key == connection_key(connection.engine.url),
                registry.c.schema_tag == schema_tag,
            )
        ).one()


def test_acknowledge_schema_migration_clears_drift(pg_db, pg_connection_config, monkeypatch, cleanup_after_test):
    db_name = f"rectify_db_{uuid.uuid4().hex[:8]}"
    schema_a = f"test_{uuid.uuid4().hex[:8]}"
    schema_b = f"test_{uuid.uuid4().hex[:8]}"

    _register(_stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name, schema=schema_a), db_name)

    stack_b = _stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name, schema=schema_b)
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


def test_acknowledge_schema_migration_refuses_a_schema_claimed_by_another_tag(
    pg_db, pg_connection_config, monkeypatch, cleanup_after_test
):
    db_name = f"rectify_db_{uuid.uuid4().hex[:8]}"
    other_tag = f"other_{uuid.uuid4().hex[:8]}"
    claimed_schema = f"test_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, [other_tag])
    with pg_db.committing_engine.begin() as connection:
        _record_schema_provenance(
            connection, database_config_name="other_entry", schema_tag=other_tag,
            new_physical_schema=claimed_schema, reason="claimed by another tag",
        )

    stack = _stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name, schema=f"test_{uuid.uuid4().hex[:8]}")
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack)
    result = runner.invoke(
        app,
        [
            "acknowledge-schema-migration",
            "--database", db_name,
            "--schema-tag", "primary",
            "--new-schema", claimed_schema,
            "--reason", "test acknowledgment onto a claimed schema",
        ],
    )
    assert result.exit_code != 0
    assert "Refusing to acknowledge" in result.output
    assert f"other_entry ({other_tag})" in result.output


def test_acknowledge_schema_migration_records_the_acknowledging_entry(pg_db, pg_connection_config, monkeypatch):
    db_name_a = f"rectify_db_{uuid.uuid4().hex[:8]}"
    db_name_b = f"rectify_db_{uuid.uuid4().hex[:8]}"
    schema_a = f"test_{uuid.uuid4().hex[:8]}"
    schema_b = f"test_{uuid.uuid4().hex[:8]}"

    _register(_stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name_a, schema=schema_a), db_name_a)

    stack_b = _stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name_b, schema=schema_b)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack_b)
    assert runner.invoke(app, ["verify"]).exit_code == 1

    result = runner.invoke(
        app,
        [
            "acknowledge-schema-migration",
            "--database", db_name_b,
            "--schema-tag", "primary",
            "--new-schema", schema_b,
            "--reason", "entry b takes over",
        ],
    )
    assert result.exit_code == 0, result.output

    row = _registry_row(pg_db, "primary")
    assert (row.database_config_name, row.physical_schema, row.previous_physical_schema) == (
        db_name_b, schema_b, schema_a,
    )


def test_acknowledge_schema_migration_requires_a_reason(pg_db, pg_connection_config, monkeypatch, cleanup_after_test):
    db_name = f"rectify_db_{uuid.uuid4().hex[:8]}"
    schema = f"test_{uuid.uuid4().hex[:8]}"
    stack = _stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name, schema=schema)
    monkeypatch.setattr("oa_configurator.cli.load_stack_config", lambda: stack)

    result = runner.invoke(
        app, ["acknowledge-schema-migration", "--database", db_name, "--new-schema", schema]
    )
    assert result.exit_code != 0


def test_drop_orphan_schema_tables_previews_then_drops(pg_db, pg_connection_config, monkeypatch, cleanup_after_test):
    db_name = f"rectify_db_{uuid.uuid4().hex[:8]}"
    cdm_schema = f"test_{uuid.uuid4().hex[:8]}"
    orphan_schema = f"orphan_{uuid.uuid4().hex[:8]}"
    stack = _stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name, schema=cdm_schema)
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


def test_drop_orphan_schema_tables_refuses_a_schema_still_in_use(pg_db, pg_connection_config, monkeypatch, cleanup_after_test):
    db_name = f"rectify_db_{uuid.uuid4().hex[:8]}"
    cdm_schema = f"test_{uuid.uuid4().hex[:8]}"
    stack = _stack_with_one_cdm_db(pg_connection_config, database_config_name=db_name, schema=cdm_schema)
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
