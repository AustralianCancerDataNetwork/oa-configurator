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
import sqlalchemy as sa
from sqlalchemy import Table

from oa_configurator import (
    CDMDatabaseConfig,
    ConnectionConfig,
    Dialect,
    GenericDatabaseConfig,
    Resolver,
    Role,
    SchemaClaim,
    SchemaDriftError,
    SchemaOwnershipError,
    StackConfig,
)
from oa_configurator.domains.resources.rectify import drop_orphan_schema_tables
from oa_configurator.domains.resources.schema_registry import (
    SchemaRegistry,
    _list_registry_rows,
)
from oa_configurator.testing import (
    drop_schema_if_exists,
    isolated_test_schema,
    reset_schema_registry_rows,
)
from oa_configurator.testing.postgres import PostgresTestStrategy

pytestmark = [pytest.mark.postgresql, pytest.mark.db_dialect, pytest.mark.usefixtures("fresh_role_registry_rows")]


def test_registry_identity_ignores_loopback_spelling_for_baselines_and_orphan_checks(
    pg_db, pg_connection_config, cleanup_after_test
):
    """The in-database registry must see one claim through localhost aliases."""
    tag = f"alias_{uuid.uuid4().hex[:8]}"
    schema = f"test_{uuid.uuid4().hex[:8]}"
    database_name = f"alias_entry_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, [tag])
    cleanup_after_test(lambda: drop_schema_if_exists(pg_db.committing_engine, schema))
    configs = []
    for host in ("127.0.0.1", "localhost"):
        connection_config = pg_connection_config.model_copy(
            update={"host": host, "port": pg_connection_config.port, "test_only": True}
        )
        stack = StackConfig.for_session(
            connections={"c": connection_config},
            databases={database_name: GenericDatabaseConfig(connection="c")},
        )
        configs.append(Resolver(stack).resolve_database(database_name))

    engines = [
        resolved.create_engine(
            schema_claims=[SchemaClaim(schema_tag=tag, physical_schema=schema)], owner="test_owner"
        )
        for resolved in configs
    ]
    try:
        with engines[1].connect() as connection:
            rows = [row for row in _list_registry_rows(connection) if row.schema_tag == tag]
            assert len(rows) == 1
            assert rows[0].physical_schema == schema
            with pytest.raises(RuntimeError, match="schema-provenance records"):
                drop_orphan_schema_tables(
                    connection,
                    stack=StackConfig.for_session(connections={}, databases={}),
                    orphan_schema=schema,
                    confirm=False,
                )
    finally:
        for engine in engines:
            engine.dispose()


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
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, ["primary", tag])

    cleanup_after_test(lambda: drop_schema_if_exists(pg_db.committing_engine, schema))
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


def test_reregistering_the_same_claim_from_a_new_engine_is_a_noop(
    pg_db, pg_connection_config, cleanup_after_test
):
    schema = f"test_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, ["primary"])

    cleanup_after_test(lambda: drop_schema_if_exists(pg_db.committing_engine, schema))
    stack = StackConfig.for_session(
        connections={"c": pg_connection_config},
        databases={"default": GenericDatabaseConfig(connection="c", schema_name=schema)},
    )
    resolved = Resolver(stack).resolve_database("default")
    for _ in range(2):
        engine = resolved.create_engine()
        engine.dispose()  # must not raise on the second call


def test_create_engine_raises_schema_drift_for_the_same_entry_on_a_production_connection(
    pg_db, pg_connection_config,
):
    """create_engines() raises SchemaDriftError directly on a second call
    with a differing schema for the same entry, on a production
    (non-test_only) connection."""
    db_name = f"drift_test_{uuid.uuid4().hex[:8]}"
    with (
        isolated_test_schema(pg_db.committing_engine) as schema_a,
        isolated_test_schema(pg_db.committing_engine) as schema_b,
    ):
        stack_a = StackConfig.for_session(
            connections={"c": pg_connection_config},
            databases={db_name: CDMDatabaseConfig(connection="c", cdm_schema=schema_a)},
        )
        engine_a, _ = Resolver(stack_a).resolve_database(db_name).create_engines()
        engine_a.dispose()

        stack_b = StackConfig.for_session(
            connections={"c": pg_connection_config},
            databases={db_name: CDMDatabaseConfig(connection="c", cdm_schema=schema_b)},
        )
        with pytest.raises(SchemaDriftError, match=f"{schema_b!r}.*{schema_a!r}"):
            Resolver(stack_b).resolve_database(db_name).create_engines()


def test_vocab_only_connection_does_not_register_primary_or_results(
    pg_db, pg_connection_config, cleanup_after_test
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
    vocab_schema = f"test_{uuid.uuid4().hex[:8]}"
    with isolated_test_schema(pg_db.committing_engine) as cdm_schema:
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

    cleanup_after_test(lambda: drop_schema_if_exists(pg_db.committing_engine, schema_a))
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

    cleanup_after_test(lambda: drop_schema_if_exists(pg_db.committing_engine, reserved))

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
    schema_name, via ResolvedCDMDatabase.create_engines()."""
    reserved = f"reserved_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, [reserved])

    cleanup_after_test(lambda: drop_schema_if_exists(pg_db.committing_engine, reserved))

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
        colliding_resolved.create_engines()


def test_create_engine_without_registering_claims_writes_no_rows(pg_db, pg_connection_config, cleanup_after_test):
    """register_claims=False builds the same translate map but leaves the
    schema_registry untouched."""
    schema = f"test_{uuid.uuid4().hex[:8]}"
    tag = f"custom_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, ["primary", tag])

    cleanup_after_test(lambda: drop_schema_if_exists(pg_db.committing_engine, schema))
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
    with isolated_test_schema(pg_db.committing_engine, prefix="reserved") as reserved:
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


def test_role_tag_claim_is_rejected_on_a_generic_database_before_registration(
    pg_db, pg_connection_config
):
    """Role-shaped tags cannot be registered as caller claims on generic entries."""
    db_name = f"role_tag_{uuid.uuid4().hex[:8]}"
    stack = StackConfig.for_session(
        connections={"c": pg_connection_config},
        databases={db_name: GenericDatabaseConfig(connection="c")},
    )
    resolved = Resolver(stack).resolve_database(db_name)
    with pytest.raises(ValueError, match="CDM role tag.*generic database"):
        resolved.create_engine(
            schema_claims=[SchemaClaim(schema_tag="vocab", physical_schema="caller_schema")],
            owner="owner-a",
        )

    table = cast(Table, SchemaRegistry.__table__)
    with pg_db.connection.engine.connect() as connection:
        rows = connection.execute(
            table.select().where(
                table.c.schema_tag == "vocab", table.c.database_config_name == db_name
            )
        ).all()
    assert rows == []


def test_both_engines_agree_on_every_shared_tag(
    pg_db, pg_connection_config, cleanup_after_test
):
    """With cdm_schema and vocab_schema both unset, each role's schema comes
    from its own connection's live default. Before the schemas were resolved
    once per database, each engine filled the other side's tags from its own
    connection, so the two maps disagreed about where every shared tag lived.
    """
    db_name = f"agree_{uuid.uuid4().hex[:8]}"
    vocab_database = f"vocab_agree_{uuid.uuid4().hex[:8]}"
    vocab_home = f"vocab_home_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, ["primary", "results"])

    primary_connection_config = pg_connection_config.model_copy(update={"test_only": True})
    vocab_connection_config = pg_connection_config.model_copy(
        update={"database_name": vocab_database, "test_only": True}
    )
    strategy = PostgresTestStrategy()
    strategy._ensure_test_db_exists(vocab_connection_config.build_url())
    cleanup_after_test(
        lambda: strategy.drop_test_database(vocab_connection_config.resolve("vocab"))
    )
    # A different default schema on the vocabulary database, so "ask that
    # connection" and "assume this engine's default" cannot coincide.
    admin = sa.create_engine(vocab_connection_config.build_url(), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {vocab_home}"))
            connection.execute(
                sa.text(f'ALTER DATABASE "{vocab_database}" SET search_path TO {vocab_home}')
            )
    finally:
        admin.dispose()

    stack = StackConfig.for_session(
        connections={"primary": primary_connection_config, "vocab": vocab_connection_config},
        databases={
            db_name: CDMDatabaseConfig(connection="primary", vocab_connection="vocab")
        },
    )
    resolved = Resolver(stack).resolve_database(db_name)
    assert resolved.vocab_schema is None
    assert resolved.resolved_physical_schemas()["vocab"] == vocab_home

    primary_engine, vocab_engine = resolved.create_engines()
    try:
        primary_map = primary_engine.get_execution_options()["schema_translate_map"]
        vocab_map = vocab_engine.get_execution_options()["schema_translate_map"]
        shared = set(primary_map) & set(vocab_map)
        assert shared >= {"primary", "vocab", "results"}
        assert all(primary_map[tag] == vocab_map[tag] for tag in shared)
        assert primary_map["vocab"] == vocab_home
    finally:
        primary_engine.dispose()
        vocab_engine.dispose()


def test_topology_capability_answers_per_tag_pair(
    pg_db, pg_connection_config, cleanup_after_test
):
    """foreign_key_can_span and tags_share_a_transaction answer from physical
    identity, so results (which has no connection of its own) pairs with
    primary while a genuinely remote vocabulary does not."""
    db_name = f"topology_{uuid.uuid4().hex[:8]}"
    vocab_database = f"vocab_topology_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, ["primary", "results"])

    primary_connection_config = pg_connection_config.model_copy(update={"test_only": True})
    vocab_connection_config = pg_connection_config.model_copy(
        update={"database_name": vocab_database, "test_only": True}
    )
    strategy = PostgresTestStrategy()
    strategy._ensure_test_db_exists(vocab_connection_config.build_url())
    cleanup_after_test(
        lambda: strategy.drop_test_database(vocab_connection_config.resolve("vocab"))
    )

    split = Resolver(StackConfig.for_session(
        connections={"primary": primary_connection_config, "vocab": vocab_connection_config},
        databases={db_name: CDMDatabaseConfig(connection="primary", vocab_connection="vocab")},
    )).resolve_database(db_name)
    assert split.foreign_key_can_span(Role.PRIMARY, Role.RESULTS) is True
    assert split.foreign_key_can_span(Role.PRIMARY, Role.VOCAB) is False
    assert split.tags_share_a_transaction(Role.PRIMARY, Role.VOCAB) is False
    assert split.tags_share_a_transaction(Role.PRIMARY) is True
    assert split.tags_share_a_transaction() is True
    # A custom tag routes to primary, so it pairs with primary and not vocab.
    assert split.foreign_key_can_span("cdm_ext", Role.PRIMARY) is True
    assert split.foreign_key_can_span("cdm_ext", Role.VOCAB) is False

    colocated = Resolver(StackConfig.for_session(
        connections={"primary": primary_connection_config},
        databases={db_name: CDMDatabaseConfig(connection="primary")},
    )).resolve_database(db_name)
    assert colocated.foreign_key_can_span(Role.PRIMARY, Role.VOCAB) is True
    assert colocated.tags_share_a_transaction(Role.PRIMARY, Role.VOCAB, Role.RESULTS) is True


def test_role_hosted_on_a_schemaless_connection_stays_unqualified(
    pg_db, pg_connection_config, cleanup_after_test, tmp_path
):
    """A vocab connection with no real schema concept resolves vocab_schema
    to None. That None belongs in the primary engine's translate map
    verbatim: the primary connection's own default schema is not where the
    vocab tables live, so filling it in would point every vocab reference at
    the wrong database's public schema."""
    cdm_schema = f"test_{uuid.uuid4().hex[:8]}"
    db_name = f"schemaless_vocab_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, ["primary", "results"])
    cleanup_after_test(lambda: drop_schema_if_exists(pg_db.committing_engine, cdm_schema))

    primary_connection_config = pg_connection_config.model_copy(update={"test_only": True})
    stack = StackConfig.for_session(
        connections={
            "primary": primary_connection_config,
            "vocab": ConnectionConfig(
                dialect=Dialect.SQLITE,
                database_name=str(tmp_path / "vocab.db"),
                test_only=True,
            ),
        },
        databases={
            db_name: CDMDatabaseConfig(
                connection="primary", vocab_connection="vocab", cdm_schema=cdm_schema,
            )
        },
    )
    resolved = Resolver(stack).resolve_database(db_name)
    assert resolved.vocab_schema is None

    engine, vocab_engine = resolved.create_engines()
    try:
        translate_map = engine.get_execution_options()["schema_translate_map"]
    finally:
        engine.dispose()
        vocab_engine.dispose()

    assert translate_map["primary"] == cdm_schema
    assert translate_map["vocab"] is None


def test_two_connection_entries_for_one_database_are_not_a_split(
    pg_db, pg_connection_config, cleanup_after_test
):
    """Split is decided by physical identity, not by which config entry a role
    names. Two entries addressing one database collapse to a single engine, so
    a foreign key between a primary-side and a vocabulary-side table stays
    creatable and no second pool is opened to a database already in use."""
    db_name = f"same_db_{uuid.uuid4().hex[:8]}"
    cdm_schema = f"test_{uuid.uuid4().hex[:8]}"
    vocab_schema = f"test_{uuid.uuid4().hex[:8]}"
    reset_schema_registry_rows(cleanup_after_test, pg_db.committing_engine, ["primary", "vocab", "results"])
    for schema in (cdm_schema, vocab_schema):
        cleanup_after_test(lambda s=schema: drop_schema_if_exists(pg_db.committing_engine, s))

    # Deliberately two entries, same database, reached by different loopback
    # spellings. Keep the review server's configured port on both URLs.
    primary_connection_config = pg_connection_config.model_copy(update={"test_only": True})
    vocab_connection_config = pg_connection_config.model_copy(
        update={"test_only": True, "host": "localhost", "port": pg_connection_config.port}
    )
    stack = StackConfig.for_session(
        connections={"primary": primary_connection_config, "vocab_alias": vocab_connection_config},
        databases={
            db_name: CDMDatabaseConfig(
                connection="primary", vocab_connection="vocab_alias",
                cdm_schema=cdm_schema, vocab_schema=vocab_schema,
            )
        },
    )
    resolved = Resolver(stack).resolve_database(db_name)
    assert resolved.connection.name != resolved.vocab_connection.name
    assert resolved.connection.addresses_same_database_as(resolved.vocab_connection)

    primary, vocab = resolved.create_engines()
    try:
        assert vocab is primary
        translate_map = primary.get_execution_options()["schema_translate_map"]
        assert translate_map["primary"] == cdm_schema
        assert translate_map["vocab"] == vocab_schema
        assert set(resolved.roles_on_connection(primary)) == {Role.PRIMARY, Role.VOCAB, Role.RESULTS}
    finally:
        primary.dispose()
