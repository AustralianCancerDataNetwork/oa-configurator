"""Direct, dialect-level tests for domains/resources/rectify.py.

Distinct from test_cli_schema_rectify.py, which exercises the CLI layer
against real Postgres. This file targets rectify.py's own functions
directly, for checks that don't need a live database at all.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from oa_configurator import (
    CDMDatabaseConfig,
    ConnectionConfig,
    Resolver,
    StackConfig,
)
from oa_configurator.domains.resources.rectify import (
    drop_orphan_schema_tables,
    schema_is_a_current_target,
)

_EMPTY_STACK = StackConfig.for_session(connections={}, databases={})


def test_drop_orphan_schema_tables_refuses_a_dialect_with_no_schema_concept():
    """"Orphan schema" doesn't apply to a dialect with no real schema
    concept at all. Must fail clearly and early, before the cross-entry
    safety check or any DDL, rather than a raw error partway through."""
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        with pytest.raises(ValueError, match="no real schema concept"):
            drop_orphan_schema_tables(
                connection,
                stack=_EMPTY_STACK,
                orphan_schema="whatever",
                confirm=False,
            )


@pytest.mark.postgresql
@pytest.mark.db_dialect
def test_drop_orphan_schema_tables_does_not_touch_a_table_outside_the_orphan_schema(
    pg_db, cleanup_after_test
):
    """Reflecting the orphan schema also pulls in any table a foreign key
    points at, even one in a different, live schema (SQLAlchemy resolves
    the constraint by reflecting its target too). That table must survive:
    only what's physically inside orphan_schema gets dropped."""
    orphan_schema = f"orphan_{uuid.uuid4().hex[:8]}"
    live_schema = f"live_{uuid.uuid4().hex[:8]}"
    engine = pg_db.connection.engine

    with engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{orphan_schema}"'))
        connection.execute(sa.text(f'CREATE SCHEMA "{live_schema}"'))
        connection.execute(sa.text(f'CREATE TABLE "{live_schema}".parent (id INTEGER PRIMARY KEY)'))
        connection.execute(
            sa.text(
                f'CREATE TABLE "{orphan_schema}".child (id INTEGER PRIMARY KEY, '
                f'parent_id INTEGER REFERENCES "{live_schema}".parent(id))'
            )
        )

    def _drop_both_schemas():
        with engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{orphan_schema}" CASCADE'))
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{live_schema}" CASCADE'))

    cleanup_after_test(_drop_both_schemas)

    with engine.connect() as connection:
        drop_orphan_schema_tables(
            connection, stack=_EMPTY_STACK, orphan_schema=orphan_schema, confirm=True
        )
        connection.commit()

    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT to_regclass(:name)"), {"name": f"{live_schema}.parent"}
        ).scalar() is not None, "table outside the orphan schema must not be dropped"
        assert connection.execute(
            sa.text("SELECT to_regclass(:name)"), {"name": f"{orphan_schema}.child"}
        ).scalar() is None


@pytest.mark.postgresql
@pytest.mark.db_dialect
def test_drop_orphan_schema_tables_does_not_translate_an_orphan_named_like_a_tag(
    pg_db, pg_connection_config, cleanup_after_test
):
    """Regression for the critical live-reproduced bug: a connection built
    by create_engines() carries a live schema_translate_map, and dropping
    through metadata.drop_all() used to let its DDL compiler translate an
    orphan_schema that happened to equal a tag key (e.g. a legacy physical
    schema literally named "vocab") into the *configured* physical schema,
    destroying the wrong data. The DROP must be emitted as literal,
    qualified SQL instead, immune to the map."""
    legacy_schema = "vocab"  # deliberately equals the Role.VOCAB tag key
    configured_schema = f"omop_vocab_{uuid.uuid4().hex[:8]}"
    db_name = f"orphan_tag_{uuid.uuid4().hex[:8]}"

    def _drop_schemas():
        with pg_db.committing_engine.begin() as connection:
            for schema in (legacy_schema, configured_schema):
                connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    cleanup_after_test(_drop_schemas)

    with pg_db.committing_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{legacy_schema}"'))
        connection.execute(sa.text(f'CREATE TABLE "{legacy_schema}".concept (id INTEGER PRIMARY KEY)'))

    stack = StackConfig.for_session(
        connections={"orphan_tag_conn": pg_connection_config},
        databases={
            db_name: CDMDatabaseConfig(
                connection="orphan_tag_conn", cdm_schema=configured_schema, vocab_schema=configured_schema,
            ),
        },
    )
    resolved = Resolver(stack).resolve_database(db_name)
    # Establish the baseline for configured_schema (still empty) first,
    # then populate it, mirroring how a real deployment's schema already
    # holds data by the time someone runs this command.
    for engine in set(resolved.create_engines()):
        engine.dispose()
    with pg_db.committing_engine.begin() as connection:
        connection.execute(sa.text(f'CREATE TABLE "{configured_schema}".concept (id INTEGER PRIMARY KEY)'))

    engine, _ = resolved.create_engines()  # carries schema_translate_map, including {"vocab": configured_schema}
    try:
        with engine.begin() as connection:
            drop_orphan_schema_tables(connection, stack=stack, orphan_schema=legacy_schema, confirm=True)
    finally:
        engine.dispose()

    with pg_db.committing_engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT to_regclass(:name)"), {"name": f"{legacy_schema}.concept"}
        ).scalar() is None, "the orphan table itself must be dropped"
        assert connection.execute(
            sa.text("SELECT to_regclass(:name)"), {"name": f"{configured_schema}.concept"}
        ).scalar() is not None, "the live, configured schema must survive untouched"


@pytest.mark.postgresql
@pytest.mark.db_dialect
def test_schema_is_a_current_target_never_leaks_a_role_from_a_different_server(pg_db, pg_connection_config):
    """A CDM database's vocab_schema must never be reported as a current
    target of a connection that is genuinely a different physical server
    than vocab_connection. 
    
    This is a regression test for occupied_schemas()
    unconditionally including every role's schema regardless of which
    connection was actually passed in.

    vocab_connection here is a deliberately unreachable host: roles_on_connection()
    never opens it, only compares URL fields, so this needs no real second server.
    """
    vocab_schema_name = f"vocab_{uuid.uuid4().hex[:8]}"
    cdm_schema_name = f"cdm_{uuid.uuid4().hex[:8]}"
    stack = StackConfig.for_session(
        connections={
            "primary": pg_connection_config,
            "vocab": ConnectionConfig(
                dialect="postgresql+psycopg",
                host="unreachable-vocab-host.invalid",
                port=5432,
                user="nobody",
                password="nothing",
                database_name="unreachable",
            ),
        },
        databases={
            "cdm": CDMDatabaseConfig(
                connection="primary",
                vocab_connection="vocab",
                cdm_schema=cdm_schema_name,
                vocab_schema=vocab_schema_name,
            ),
        },
    )
    resolved = Resolver(stack).resolve_database("cdm")

    with pg_db.connection.engine.connect() as connection:
        assert resolved.occupied_schemas(connection) == {cdm_schema_name}
        assert schema_is_a_current_target(connection, stack, vocab_schema_name) is None
        assert schema_is_a_current_target(connection, stack, cdm_schema_name) == "cdm"
