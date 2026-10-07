"""Live-Postgres regression for create_engine()'s schema-registry wiring:
the full path through create_engine() itself, including owner
auto-derivation from the call stack and the SchemaOwnershipError raised
before any DDL runs.

Every test builds its engines from pg_connection_config, never the
rollback-protected pg_db.connection, since registration writes must
actually commit to be checked by a later call. reset_schema_registry_rows
restores each test's own tags, and fresh_role_registry_rows the Role tags.
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from oa_configurator import (
    CDMDatabaseConfig,
    GenericDatabaseConfig,
    Resolver,
    SchemaClaim,
    SchemaDriftError,
    SchemaOwnershipError,
    StackConfig,
)
from oa_configurator.domains.resources.schema_registry import SchemaRegistry
from oa_configurator.testing import reset_schema_registry_rows
from sqlalchemy import Table

pytestmark = [pytest.mark.postgresql, pytest.mark.db_dialect, pytest.mark.usefixtures("fresh_role_registry_rows")]


def test_create_engine_derives_owner_from_the_caller_without_being_told(pg_db, pg_connection_config, cleanup_after_test):
    """A resolver-owned Role claim (schema_name here) always records
    owner=None: the same tag string means something different on every
    database entry, so tying it to whichever package called create_engine()
    would produce false cross-database ownership conflicts. Owner
    derivation is only observable on a custom schema_claims entry, which
    does get attributed to the caller."""
    schema = f"test_{uuid.uuid4().hex[:8]}"
    tag = f"custom_{uuid.uuid4().hex[:8]}"
    db_name = f"owner_test_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, [tag])
    stack = StackConfig.for_session(
        connections={"c": pg_connection_config},
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


def test_reregistering_the_same_claim_from_a_new_engine_is_a_noop(pg_connection_config):
    schema = f"test_{uuid.uuid4().hex[:8]}"
    stack = StackConfig.for_session(
        connections={"c": pg_connection_config},
        databases={"default": GenericDatabaseConfig(connection="c", schema_name=schema)},
    )
    resolved = Resolver(stack).resolve_database("default")
    for _ in range(2):
        engine = resolved.create_engine()
        engine.dispose()  # must not raise on the second call


def test_create_engine_raises_schema_drift_for_the_same_entry_on_a_production_connection(
    pg_connection_config,
):
    """create_engine() raises SchemaDriftError directly on a second call
    with a differing schema for the same entry, on a production
    (non-test_only) connection."""
    db_name = f"drift_test_{uuid.uuid4().hex[:8]}"
    schema_a = f"test_{uuid.uuid4().hex[:8]}"
    schema_b = f"test_{uuid.uuid4().hex[:8]}"

    stack_a = StackConfig.for_session(
        connections={"c": pg_connection_config},
        databases={db_name: CDMDatabaseConfig(connection="c", cdm_schema=schema_a)},
    )
    engine_a = Resolver(stack_a).resolve_database(db_name).create_engine()
    engine_a.dispose()

    stack_b = StackConfig.for_session(
        connections={"c": pg_connection_config},
        databases={db_name: CDMDatabaseConfig(connection="c", cdm_schema=schema_b)},
    )
    with pytest.raises(SchemaDriftError, match=f"{schema_b!r}.*{schema_a!r}"):
        Resolver(stack_b).resolve_database(db_name).create_engine()


def test_vocab_only_connection_does_not_register_primary_or_results(
    pg_connection_config, cleanup_after_test
):
    """A split-CDM's vocab-only engine must not register primary/results
    in its own schema_registry, only fold them into its translate_map.
    Regression for local_roles in _process_schema_claims.

    Uses a second real database on the same Postgres server, since a
    different schema on the same connection wouldn't exercise this.
    """
    from oa_configurator.domains.resources.schema_registry import _list_registry_rows
    from oa_configurator.testing.postgres import PostgresTestStrategy

    db_name = f"split_test_{uuid.uuid4().hex[:8]}"
    cdm_schema = f"test_{uuid.uuid4().hex[:8]}"
    vocab_schema = f"test_{uuid.uuid4().hex[:8]}"
    # Both must be test_only=True: StackConfig requires every connection one
    # CDM entry references to agree, and drop_test_database() requires it.
    primary_connection_config = pg_connection_config.model_copy(update={"test_only": True})
    vocab_connection_config = pg_connection_config.model_copy(
        update={"database_name": f"vocab_test_{uuid.uuid4().hex[:8]}", "test_only": True}
    )

    strategy = PostgresTestStrategy()
    strategy._ensure_test_db_exists(vocab_connection_config.build_url())
    cleanup_after_test(lambda: strategy.drop_test_database(vocab_connection_config.resolve("vocab")))

    stack = StackConfig.for_session(
        connections={"primary": primary_connection_config, "vocab": vocab_connection_config},
        databases={
            db_name: CDMDatabaseConfig(
                connection="primary", vocab_connection="vocab",
                cdm_schema=cdm_schema, vocab_schema=vocab_schema,
            )
        },
    )
    resolved = Resolver(stack).resolve_database(db_name)
    primary_engine, vocab_engine = resolved.create_engines()
    role_tags = {"primary", "vocab", "results"}
    try:
        with vocab_engine.connect() as connection:
            vocab_role_rows = {
                row.schema_tag for row in _list_registry_rows(connection)
                if row.database_config_name == db_name
            } & role_tags
        with primary_engine.connect() as connection:
            primary_role_rows = {
                row.schema_tag for row in _list_registry_rows(connection)
                if row.database_config_name == db_name
            } & role_tags
        # Both connections still compile cross-schema references correctly,
        # even though each only registered the role(s) it actually owns.
        vocab_map = vocab_engine.get_execution_options()["schema_translate_map"]
        assert vocab_map["primary"] == cdm_schema
        assert vocab_map["vocab"] == vocab_schema
    finally:
        primary_engine.dispose()
        vocab_engine.dispose()

    assert vocab_role_rows == {"vocab"}
    assert primary_role_rows == {"primary", "results"}


def test_two_databases_reusing_one_tag_on_one_connection_with_different_schemas_raises(
    pg_db, pg_connection_config, cleanup_after_test
):
    """A genuine ownership collision: two configs, one shared connection,
    the same custom schema_translate_map tag pointed at two different
    physical schemas."""
    tag = f"custom_{uuid.uuid4().hex[:8]}"
    schema_a = f"test_{uuid.uuid4().hex[:8]}"
    schema_b = f"test_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, [tag])
    stack = StackConfig.for_session(
        connections={"c": pg_connection_config},
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
    pg_db, pg_connection_config, cleanup_after_test
):
    """A package reserves a physical schema for its own internal use; a
    second, unrelated database's own configured schema colliding with it
    is caught at create_engine() time."""
    reserved = f"reserved_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, [reserved])

    owner_stack = StackConfig.for_session(
        connections={"c": pg_connection_config},
        databases={"owner_db": GenericDatabaseConfig(connection="c")},
    )
    owner_resolved = Resolver(owner_stack).resolve_database("owner_db")
    owner_engine = owner_resolved.create_engine(
        owner="reserving-pkg",
        schema_claims=[SchemaClaim(schema_tag=reserved, physical_schema=reserved, reserved=True)],
    )
    owner_engine.dispose()

    colliding_stack = StackConfig.for_session(
        connections={"c": pg_connection_config},
        databases={"other_db": GenericDatabaseConfig(connection="c", schema_name=reserved)},
    )
    colliding_resolved = Resolver(colliding_stack).resolve_database("other_db")
    with pytest.raises(SchemaOwnershipError, match=f"{reserved!r}.*reserving-pkg"):
        colliding_resolved.create_engine()


def test_cdm_database_reserved_vocab_schema_collision_raises(pg_db, pg_connection_config, cleanup_after_test):
    """Defense in depth for a CDMDatabaseConfig: vocab_schema colliding with
    a reservation is caught the same way as GenericDatabaseConfig's
    schema_name, via ResolvedCDMDatabase.create_engine()."""
    reserved = f"reserved_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, [reserved])

    owner_stack = StackConfig.for_session(
        connections={"c": pg_connection_config},
        databases={"owner_db": GenericDatabaseConfig(connection="c")},
    )
    owner_engine = Resolver(owner_stack).resolve_database("owner_db").create_engine(
        owner="reserving-pkg",
        schema_claims=[SchemaClaim(schema_tag=reserved, physical_schema=reserved, reserved=True)],
    )
    owner_engine.dispose()

    colliding_stack = StackConfig.for_session(
        connections={"c": pg_connection_config},
        databases={
            "cdm_db": CDMDatabaseConfig(connection="c", cdm_schema="omop", vocab_schema=reserved)
        },
    )
    colliding_resolved = Resolver(colliding_stack).resolve_database("cdm_db")
    with pytest.raises(SchemaOwnershipError, match=f"{reserved!r}.*reserving-pkg"):
        colliding_resolved.create_engine()


def test_create_engine_without_registering_claims_writes_no_rows(pg_db, pg_connection_config, cleanup_after_test):
    """register_claims=False builds the same translate map but leaves the
    schema_registry untouched."""
    schema = f"test_{uuid.uuid4().hex[:8]}"
    tag = f"custom_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, [tag])
    stack = StackConfig.for_session(
        connections={"c": pg_connection_config},
        databases={"default": GenericDatabaseConfig(connection="c", schema_name=schema)},
    )
    resolved = Resolver(stack).resolve_database("default")
    claims = [SchemaClaim(schema_tag=tag, physical_schema=schema)]

    unregistered = resolved.create_engine(schema_claims=claims, register_claims=False)
    unregistered_map = unregistered.get_execution_options()["schema_translate_map"]
    unregistered.dispose()

    table = cast(Table, SchemaRegistry.__table__)
    with pg_db.connection.engine.connect() as connection:
        rows = connection.execute(table.select().where(table.c.physical_schema == schema)).all()
    assert rows == []

    registered = resolved.create_engine(schema_claims=claims)
    assert registered.get_execution_options()["schema_translate_map"] == unregistered_map
    registered.dispose()


def test_create_engine_without_registering_claims_still_rejects_a_reserved_schema(
    pg_db, pg_connection_config, cleanup_after_test
):
    reserved = f"reserved_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, [reserved])

    owner_stack = StackConfig.for_session(
        connections={"c": pg_connection_config},
        databases={"owner_db": GenericDatabaseConfig(connection="c")},
    )
    owner_engine = Resolver(owner_stack).resolve_database("owner_db").create_engine(
        owner="reserving-pkg",
        schema_claims=[SchemaClaim(schema_tag=reserved, physical_schema=reserved, reserved=True)],
    )
    owner_engine.dispose()

    colliding_stack = StackConfig.for_session(
        connections={"c": pg_connection_config},
        databases={"other_db": GenericDatabaseConfig(connection="c", schema_name=reserved)},
    )
    colliding_resolved = Resolver(colliding_stack).resolve_database("other_db")
    with pytest.raises(SchemaOwnershipError, match=f"{reserved!r}.*reserving-pkg"):
        colliding_resolved.create_engine(register_claims=False)
