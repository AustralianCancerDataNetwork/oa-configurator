"""Direct, dialect-level tests for domains/resources/rectify.py.

Distinct from test_cli_schema_rectify.py, which exercises the CLI layer
against real Postgres. This file targets rectify.py's own functions
directly, for checks that don't need a live database at all.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from oa_configurator import StackConfig
from oa_configurator.domains.resources.rectify import drop_orphan_schema_tables

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
