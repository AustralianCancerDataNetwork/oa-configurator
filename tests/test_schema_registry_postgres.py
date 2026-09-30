"""Live-Postgres regression for create_engine()'s schema-registry wiring:
the full path through create_engine() itself, including owner
auto-derivation from the call stack and the SchemaOwnershipError raised
before any DDL runs.

Every test builds its own fresh, real ConnectionConfig from pg_db's own
URL, never the rollback-protected pg_db.connection, since registration
writes must actually commit to be checked by a later call.
cleanup_after_test removes each test's own schema_registry rows.
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from oa_configurator import (
    CDMDatabaseConfig,
    ConnectionConfig,
    GenericDatabaseConfig,
    Resolver,
    SchemaClaim,
    SchemaOwnershipError,
    StackConfig,
)
from oa_configurator.domains.resources.schema_registry import SchemaRegistry
from oa_configurator.testing import delete_rows_on_cleanup
from sqlalchemy.engine import make_url
from sqlalchemy import Table

pytestmark = [pytest.mark.postgresql, pytest.mark.db_dialect]


def _connection_config(pg_db) -> ConnectionConfig:
    url = make_url(pg_db.connection.engine.url)
    return ConnectionConfig(
        dialect=url.drivername, host=url.host, port=url.port,
        user=url.username, password=url.password, database_name=url.database,
        test_only=False,
    )


def _cleanup_registry_rows(cleanup_after_test, pg_db, *schema_names: str) -> None:
    table = cast(Table, SchemaRegistry.__table__)
    delete_rows_on_cleanup(
        cleanup_after_test, pg_db.connection.engine, table,
        table.c.physical_schema.in_(schema_names),
    )


def test_create_engine_derives_owner_from_the_caller_without_being_told(pg_db, cleanup_after_test):
    """A resolver-owned Role claim (schema_name here) always records
    owner=None: the same tag string means something different on every
    database entry, so tying it to whichever package called create_engine()
    would produce false cross-database ownership conflicts. Owner
    derivation is only observable on a custom schema_claims entry, which
    does get attributed to the caller."""
    schema = f"test_{uuid.uuid4().hex[:8]}"
    tag = f"custom_{uuid.uuid4().hex[:8]}"
    # Unique database key, not "default": this file's tests share one real
    # connection, and an existing "default" row with a different physical
    # schema is left untouched as drift rather than overwritten, so a
    # fresh schema under "default" would never be read back.
    db_name = f"owner_test_{uuid.uuid4().hex[:8]}"
    _cleanup_registry_rows(cleanup_after_test, pg_db, schema)
    stack = StackConfig.for_session(
        connections={"c": _connection_config(pg_db)},
        databases={db_name: GenericDatabaseConfig(connection="c", schema_name=schema)},
    )
    resolved = Resolver(stack).resolve_database(db_name)
    engine = resolved.create_engine(
        schema_claims=[SchemaClaim(schema_tag=tag, physical_schema=schema)],
    )
    engine.dispose()

    table = cast(Table, SchemaRegistry.__table__)
    with pg_db.connection.engine.connect() as connection:
        rows = {
            row.schema_tag: row
            for row in connection.execute(
                table.select().where(table.c.physical_schema == schema)
            )
        }
    assert rows["primary"].owner is None
    # Called directly from this test module: owner derives to its own top-level module.
    assert rows[tag].owner == "test_schema_registry_postgres"


def test_reregistering_the_same_claim_from_a_new_engine_is_a_noop(pg_db, cleanup_after_test):
    schema = f"test_{uuid.uuid4().hex[:8]}"
    _cleanup_registry_rows(cleanup_after_test, pg_db, schema)
    stack = StackConfig.for_session(
        connections={"c": _connection_config(pg_db)},
        databases={"default": GenericDatabaseConfig(connection="c", schema_name=schema)},
    )
    resolved = Resolver(stack).resolve_database("default")
    for _ in range(2):
        engine = resolved.create_engine()
        engine.dispose()  # must not raise on the second call


def test_two_databases_reusing_one_tag_on_one_connection_with_different_schemas_raises(
    pg_db, cleanup_after_test
):
    """A genuine ownership collision: two configs, one shared connection,
    the same custom schema_translate_map tag pointed at two different
    physical schemas."""
    tag = f"custom_{uuid.uuid4().hex[:8]}"
    schema_a = f"test_{uuid.uuid4().hex[:8]}"
    schema_b = f"test_{uuid.uuid4().hex[:8]}"
    _cleanup_registry_rows(cleanup_after_test, pg_db, schema_a, schema_b)
    connection_config = _connection_config(pg_db)
    stack = StackConfig.for_session(
        connections={"c": connection_config},
        databases={"default": GenericDatabaseConfig(connection="c")},
    )
    resolved = Resolver(stack).resolve_database("default")

    engine_a = resolved.create_engine(
        schema_claims=[SchemaClaim(schema_tag=tag, physical_schema=schema_a)], owner="owner-a",
    )
    engine_a.dispose()

    with pytest.raises(SchemaOwnershipError, match=f"{tag!r}.*owner-a"):
        resolved.create_engine(
            schema_claims=[SchemaClaim(schema_tag=tag, physical_schema=schema_b)], owner="owner-b",
        )


def test_reserved_schema_collides_with_a_different_databases_configured_schema(
    pg_db, cleanup_after_test
):
    """A package reserves a physical schema for its own internal use; a
    second, unrelated database's own configured schema colliding with it
    is caught at create_engine() time."""
    reserved = f"reserved_{uuid.uuid4().hex[:8]}"
    _cleanup_registry_rows(cleanup_after_test, pg_db, reserved)
    connection_config = _connection_config(pg_db)

    owner_stack = StackConfig.for_session(
        connections={"c": connection_config},
        databases={"owner_db": GenericDatabaseConfig(connection="c")},
    )
    owner_resolved = Resolver(owner_stack).resolve_database("owner_db")
    owner_engine = owner_resolved.create_engine(
        owner="reserving-pkg",
        schema_claims=[SchemaClaim(schema_tag=reserved, physical_schema=reserved, reserved=True)],
    )
    owner_engine.dispose()

    colliding_stack = StackConfig.for_session(
        connections={"c": connection_config},
        databases={"other_db": GenericDatabaseConfig(connection="c", schema_name=reserved)},
    )
    colliding_resolved = Resolver(colliding_stack).resolve_database("other_db")
    with pytest.raises(SchemaOwnershipError, match=f"{reserved!r}.*reserving-pkg"):
        colliding_resolved.create_engine()


def test_cdm_database_reserved_vocab_schema_collision_raises(pg_db, cleanup_after_test):
    """Defense in depth for a CDMDatabaseConfig: vocab_schema colliding with
    a reservation is caught the same way as GenericDatabaseConfig's
    schema_name, via ResolvedCDMDatabase.create_engine()."""
    reserved = f"reserved_{uuid.uuid4().hex[:8]}"
    _cleanup_registry_rows(cleanup_after_test, pg_db, reserved)
    connection_config = _connection_config(pg_db)

    owner_stack = StackConfig.for_session(
        connections={"c": connection_config},
        databases={"owner_db": GenericDatabaseConfig(connection="c")},
    )
    owner_engine = Resolver(owner_stack).resolve_database("owner_db").create_engine(
        owner="reserving-pkg",
        schema_claims=[SchemaClaim(schema_tag=reserved, physical_schema=reserved, reserved=True)],
    )
    owner_engine.dispose()

    colliding_stack = StackConfig.for_session(
        connections={"c": connection_config},
        databases={
            "cdm_db": CDMDatabaseConfig(connection="c", cdm_schema="omop", vocab_schema=reserved)
        },
    )
    colliding_resolved = Resolver(colliding_stack).resolve_database("cdm_db")
    with pytest.raises(SchemaOwnershipError, match=f"{reserved!r}.*reserving-pkg"):
        colliding_resolved.create_engine()
