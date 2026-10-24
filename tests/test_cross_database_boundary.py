"""Cross-database boundary: the pre-execution guard and foreign-key derivation.

Both concern a split deployment, where two schema tags live on separate
physical databases. The guard tests use genuinely separate databases, since
the hazard it addresses is a statement that succeeds against a leftover copy
of the remote table rather than failing.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import ClassVar

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from oa_configurator import (
    CDMDatabaseConfig,
    ConnectionConfig,
    CrossDatabaseStatementError,
    GenericDatabaseConfig,
    Resolver,
    Role,
    SchemaClaim,
    StackConfig,
    UnenforcedForeignKey,
    UnregisteredSchemaTagError,
    unenforced_foreign_keys,
    without_cross_engine_foreign_keys,
)
from oa_configurator.testing.postgres import PostgresTestStrategy


def _cdm_tables() -> tuple[sa.MetaData, sa.Table, sa.Table, sa.Table]:
    """A miniature CDM: one vocabulary table and two primary-side tables,
    with ``cond`` carrying one foreign key to each side."""
    metadata = sa.MetaData()
    concept = sa.Table(
        "concept", metadata,
        sa.Column("concept_id", sa.Integer, primary_key=True),
        schema=Role.VOCAB.value,
    )
    person = sa.Table(
        "person", metadata,
        sa.Column("person_id", sa.Integer, primary_key=True),
        schema=Role.PRIMARY.value,
    )
    cond = sa.Table(
        "cond", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("person_id", sa.Integer, sa.ForeignKey("primary.person.person_id")),
        sa.Column("concept_id", sa.Integer, sa.ForeignKey("vocab.concept.concept_id")),
        schema=Role.PRIMARY.value,
    )
    return metadata, concept, person, cond


@pytest.fixture
def split_cdm(pg_connection_config, cleanup_after_test):
    """A CDM entry whose vocabulary role lives in its own PostgreSQL database."""
    primary_connection_config = pg_connection_config.model_copy(update={"test_only": True})
    vocab_connection_config = pg_connection_config.model_copy(
        update={"database_name": f"split_guard_{uuid.uuid4().hex[:8]}", "test_only": True}
    )
    strategy = PostgresTestStrategy()
    strategy._ensure_test_db_exists(vocab_connection_config.build_url())
    cleanup_after_test(
        lambda: strategy.drop_test_database(vocab_connection_config.resolve("vocab"))
    )
    name = f"split_cdm_{uuid.uuid4().hex[:8]}"
    stack = StackConfig.for_session(
        connections={"primary": primary_connection_config, "vocab": vocab_connection_config},
        databases={
            name: CDMDatabaseConfig(
                connection="primary", vocab_connection="vocab",
                cdm_schema="public", vocab_schema="public",
            )
        },
    )
    return Resolver(stack).resolve_database(name)


@pytest.fixture
def compile_split_cdm():
    stack = StackConfig.for_session(
        connections={
            "primary": oa_connection(":memory:"),
            "vocab": oa_connection(":memory:"),
        },
        databases={
            "compile_split": CDMDatabaseConfig(
                connection="primary", vocab_connection="vocab"
            )
        },
    )
    return Resolver(stack).resolve_database("compile_split")


def oa_connection(database_name: str):
    return ConnectionConfig(
        dialect="sqlite", database_name=database_name, test_only=True
    )


@pytest.fixture
def duckdb_cdm(tmp_path):
    stack = StackConfig.for_session(
        connections={
            "db": ConnectionConfig(
                dialect="duckdb", database_name=str(tmp_path / "cdm.duckdb"), test_only=True
            )
        },
        databases={
            "cdm": CDMDatabaseConfig(
                connection="db", cdm_schema="clinical", vocab_schema="vocabulary",
                results_schema="results",
            )
        },
    )
    return Resolver(stack).resolve_database("cdm")


class TestForeignKeyPolicy:
    @pytest.mark.parametrize(
        "dialect, primary_schema, vocab_schema, expected",
        [
            ("duckdb", "clinical", "vocabulary", False),
            ("duckdb", "main", "main", True),
            ("postgresql", "clinical", "vocabulary", True),
            ("sqlite", None, None, True),
        ],
    )
    def test_feasibility_uses_physical_schemas(
        self, dialect, primary_schema, vocab_schema, expected
    ):
        stack = StackConfig.for_session(
            connections={
                "db": ConnectionConfig(
                    dialect=dialect, host="localhost", database_name="unused", test_only=True
                )
            },
            databases={
                "cdm": CDMDatabaseConfig(
                    connection="db", cdm_schema=primary_schema, vocab_schema=vocab_schema
                )
            },
        )
        resolved = Resolver(stack).resolve_database("cdm")
        assert resolved.foreign_key_can_span(Role.PRIMARY, Role.VOCAB) is expected
        assert resolved.tags_share_a_transaction(Role.PRIMARY, Role.VOCAB) is True

    def test_cross_schema_report_matches_copies(self, duckdb_cdm):
        metadata, concept, person, cond = _cdm_tables()
        original = str(CreateTable(cond).compile(dialect=postgresql.dialect()))
        report = unenforced_foreign_keys(metadata.tables.values(), resolved=duckdb_cdm)
        constraint = next(fk for fk in cond.foreign_key_constraints if "concept_id" in fk.columns)
        assert report == (UnenforcedForeignKey(constraint, "cross_schema"),)

        *_, copied = without_cross_engine_foreign_keys(
            (concept, person, cond), resolved=duckdb_cdm
        )
        assert [tuple(fk.columns.keys()) for fk in copied.foreign_key_constraints] == [("person_id",)]
        assert str(CreateTable(cond).compile(dialect=postgresql.dialect())) == original
        assert len(cond.foreign_keys) == 2
        join = sa.select(cond).join(concept).get_final_froms()[0]
        assert join.onclause.compare(cond.c.concept_id == concept.c.concept_id)

    def test_cross_database_reason_does_not_require_referred_table_in_batch(self, compile_split_cdm):
        _, _, person, cond = _cdm_tables()
        report = unenforced_foreign_keys((person, cond), resolved=compile_split_cdm)
        assert len(report) == 1
        assert report[0].reason == "cross_database"
        assert report[0].constraint.table is cond

    def test_colocated_roles_keep_every_key(self, duckdb_cdm):
        resolved = replace(duckdb_cdm, vocab_schema="clinical")
        metadata, concept, person, cond = _cdm_tables()
        assert unenforced_foreign_keys(metadata.tables.values(), resolved=resolved) == ()
        *_, copied = without_cross_engine_foreign_keys((concept, person, cond), resolved=resolved)
        assert len(copied.foreign_keys) == 2

    def test_composite_key_is_reported_and_removed_as_one_constraint(self, duckdb_cdm):
        metadata = sa.MetaData()
        parent = sa.Table(
            "parent", metadata, sa.Column("a", sa.Integer, primary_key=True),
            sa.Column("b", sa.Integer, primary_key=True), schema="vocab",
        )
        child = sa.Table(
            "child", metadata, sa.Column("a", sa.Integer), sa.Column("b", sa.Integer),
            sa.ForeignKeyConstraint(["a", "b"], ["vocab.parent.a", "vocab.parent.b"]),
            schema="primary",
        )
        report = unenforced_foreign_keys((parent, child), resolved=duckdb_cdm)
        assert len(report) == 1
        assert tuple(report[0].constraint.columns.keys()) == ("a", "b")
        _, copied = without_cross_engine_foreign_keys((parent, child), resolved=duckdb_cdm)
        assert not copied.foreign_keys
        assert len(child.foreign_keys) == 2

    def test_custom_tags_require_a_resolved_mapping(self, duckdb_cdm):
        with pytest.raises(UnregisteredSchemaTagError, match="physical_schemas"):
            duckdb_cdm.foreign_key_can_span("custom", Role.PRIMARY)
        assert duckdb_cdm.foreign_key_can_span(
            "custom", Role.PRIMARY, physical_schemas={"custom": "clinical"}
        )
        assert not duckdb_cdm.foreign_key_can_span(
            "custom", Role.PRIMARY, physical_schemas={"custom": "another_schema"}
        )

    def test_default_schema_mapping_does_not_probe_an_engine(self, duckdb_cdm, monkeypatch):
        resolved = replace(duckdb_cdm, schema_name=None, vocab_schema=None, results_schema=None)
        def fail_probe(*args, **kwargs):
            pytest.fail("FK feasibility must not open a second DuckDB engine")
        monkeypatch.setattr(type(resolved), "_probe_connection_default", fail_probe)
        metadata = sa.MetaData()
        parent = sa.Table(
            "parent", metadata, sa.Column("id", sa.Integer, primary_key=True), schema="custom"
        )
        child = sa.Table(
            "child", metadata, sa.Column("parent_id", sa.ForeignKey("custom.parent.id")),
            schema="primary",
        )
        for physical_schema in ("main", None):
            schemas = {"custom": physical_schema}
            assert resolved.foreign_key_can_span("custom", Role.PRIMARY, physical_schemas=schemas)
            assert unenforced_foreign_keys((parent, child), resolved=resolved, physical_schemas=schemas) == ()
            _, copied = without_cross_engine_foreign_keys(
                (parent, child), resolved=resolved, physical_schemas=schemas
            )
            assert len(copied.foreign_keys) == 1

    @pytest.mark.parametrize(
        "primary_schema, vocab_schema, expected",
        [("CDM", "cdm", True), ("Ä", "ä", False)],
    )
    def test_duckdb_schema_comparison_uses_ascii_case_rules(
        self, duckdb_cdm, primary_schema, vocab_schema, expected
    ):
        resolved = replace(duckdb_cdm, schema_name=primary_schema, vocab_schema=vocab_schema)
        assert resolved.foreign_key_can_span(Role.PRIMARY, Role.VOCAB) is expected

    def test_custom_tag_mapping_is_shared_by_reporting_and_copying(self, duckdb_cdm):
        metadata = sa.MetaData()
        parent = sa.Table("parent", metadata, sa.Column("id", sa.Integer, primary_key=True), schema="custom")
        child = sa.Table(
            "child", metadata, sa.Column("parent_id", sa.ForeignKey("custom.parent.id")),
            schema="primary",
        )
        schemas = {"custom": "another_schema"}
        report = unenforced_foreign_keys((parent, child), resolved=duckdb_cdm, physical_schemas=schemas)
        assert [item.reason for item in report] == ["cross_schema"]
        _, copied = without_cross_engine_foreign_keys(
            (parent, child), resolved=duckdb_cdm, physical_schemas=schemas
        )
        assert not copied.foreign_keys

    def test_cycles_and_explicitly_deferred_keys_are_reported(self, duckdb_cdm):
        metadata = sa.MetaData()
        a = sa.Table(
            "a", metadata, sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("b_id", sa.ForeignKey("primary.b.id")), schema="primary",
        )
        b = sa.Table(
            "b", metadata, sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("a_id", sa.ForeignKey("primary.a.id")), schema="primary",
        )
        self_ref = sa.Table(
            "self_ref", metadata, sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("parent_id", sa.ForeignKey("primary.self_ref.id", use_alter=True)),
            schema="primary",
        )
        report = unenforced_foreign_keys((a, b, self_ref), resolved=duckdb_cdm)
        assert [item.reason for item in report] == ["deferred"] * 3
        assert {item.constraint.table for item in report} == {a, b, self_ref}
        assert all(item.constraint._create_rule is None for item in report)
        *_, copied = without_cross_engine_foreign_keys((a, b, self_ref), resolved=duckdb_cdm)
        assert len(copied.foreign_keys) == 1
        assert next(iter(copied.foreign_key_constraints)).use_alter is True

    def test_boundary_removal_breaks_cycles_before_deferred_analysis(self, duckdb_cdm):
        metadata = sa.MetaData()
        a = sa.Table(
            "a", metadata, sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("b_id", sa.ForeignKey("primary.b.id")), schema="primary",
        )
        b = sa.Table(
            "b", metadata, sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("v_id", sa.ForeignKey("vocab.v.id")), schema="primary",
        )
        v = sa.Table(
            "v", metadata, sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("a_id", sa.ForeignKey("primary.a.id")), schema="vocab",
        )
        report = unenforced_foreign_keys((a, b, v), resolved=duckdb_cdm)
        assert [item.reason for item in report] == ["cross_schema", "cross_schema"]
        assert {item.constraint.table for item in report} == {b, v}

    @pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
    def test_other_dialects_do_not_report_deferred_keys(self, dialect):
        stack = StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect=dialect, host="localhost", database_name="unused")},
            databases={"cdm": CDMDatabaseConfig(connection="db")},
        )
        resolved = Resolver(stack).resolve_database("cdm")
        metadata = sa.MetaData()
        table = sa.Table(
            "t", metadata, sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("parent_id", sa.ForeignKey("primary.t.id", use_alter=True)), schema="primary",
        )
        assert unenforced_foreign_keys((table,), resolved=resolved) == ()

    @pytest.mark.duckdb
    @pytest.mark.db_dialect
    @pytest.mark.parametrize("vocab_schema", ["clinical", "vocabulary", "CLINICAL"])
    def test_real_duckdb_ddl_preserves_supported_constraints(self, duckdb_cdm, vocab_schema):
        resolved = replace(duckdb_cdm, vocab_schema=vocab_schema)
        metadata, concept, person, cond = _cdm_tables()
        for table in metadata.tables.values():
            for column in table.primary_key:
                column.autoincrement = False
        copied = without_cross_engine_foreign_keys((concept, person, cond), resolved=resolved)
        engine, vocab_engine = resolved.create_engines()
        try:
            copied[0].metadata.create_all(engine, tables=copied)
            with engine.begin() as connection:
                connection.execute(person.insert(), {"person_id": 1})
                connection.execute(concept.insert(), {"concept_id": 1})
                connection.execute(cond.insert(), {"id": 1, "person_id": 1, "concept_id": 1})
                reflected = sa.inspect(connection).get_foreign_keys("cond", schema="clinical")
                assert len(reflected) == (2 if vocab_schema.lower() == "clinical" else 1)
            with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
                connection.execute(cond.insert(), {"id": 2, "person_id": 999, "concept_id": 1})
        finally:
            for owned in {engine, vocab_engine}:
                owned.dispose()

    @pytest.mark.duckdb
    @pytest.mark.db_dialect
    def test_unset_duckdb_schema_matches_explicit_main(self, duckdb_cdm):
        resolved = replace(duckdb_cdm, schema_name=None, vocab_schema="main", results_schema=None)
        assert resolved.foreign_key_can_span(Role.PRIMARY, Role.VOCAB)

    @pytest.mark.postgresql
    @pytest.mark.db_dialect
    def test_postgresql_enforces_cross_schema_keys(self, pg_db, pg_connection_config):
        primary_schema = f"fk_primary_{uuid.uuid4().hex[:8]}"
        vocab_schema = f"fk_vocab_{uuid.uuid4().hex[:8]}"
        stack = StackConfig.for_session(
            connections={"db": pg_connection_config},
            databases={
                "cdm": CDMDatabaseConfig(
                    connection="db", cdm_schema=primary_schema,
                    vocab_schema=vocab_schema, results_schema=primary_schema,
                )
            },
        )
        resolved = Resolver(stack).resolve_database("cdm")
        metadata, concept, person, cond = _cdm_tables()
        assert unenforced_foreign_keys(metadata.tables.values(), resolved=resolved) == ()
        copied = without_cross_engine_foreign_keys((concept, person, cond), resolved=resolved)
        connection = pg_db.connection.execution_options(
            schema_translate_map={"primary": primary_schema, "vocab": vocab_schema}
        )
        # Schemas and tables belong to pg_db's rolled-back test transaction.
        connection.execute(sa.schema.CreateSchema(primary_schema))
        connection.execute(sa.schema.CreateSchema(vocab_schema))
        copied[0].metadata.create_all(connection, tables=copied)
        reflected = sa.inspect(connection).get_foreign_keys("cond", schema=primary_schema)
        assert len(reflected) == 2
        connection.execute(person.insert(), {"person_id": 1})
        connection.execute(concept.insert(), {"concept_id": 1})
        connection.execute(cond.insert(), {"id": 1, "person_id": 1, "concept_id": 1})
        with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
            connection.execute(cond.insert(), {"id": 2, "person_id": 1, "concept_id": 999})


class TestForeignKeyDerivation:
    def test_only_the_cross_boundary_key_is_dropped(self, compile_split_cdm):
        """The vocabulary foreign key goes; the primary-side one stays. The
        rule is "drop keys that cross an engine boundary", not "drop keys"."""
        _, _, person, cond = _cdm_tables()
        _, derived = without_cross_engine_foreign_keys((person, cond), resolved=compile_split_cdm)

        dialect = postgresql.dialect()
        original_ddl = str(CreateTable(cond).compile(dialect=dialect))
        derived_ddl = str(CreateTable(derived).compile(dialect=dialect))
        assert original_ddl.count("FOREIGN KEY") == 2
        assert derived_ddl.count("FOREIGN KEY") == 1
        assert "person" in derived_ddl
        assert [c.name for c in derived.columns] == [c.name for c in cond.columns]
        assert [str(c.type) for c in derived.columns] == [str(c.type) for c in cond.columns]

    def test_nothing_is_dropped_on_a_colocated_deployment(
        self
    ):
        """Same models, one database: every foreign key is creatable, so the
        derivation must leave the table alone."""
        stack = StackConfig.for_session(
            connections={"primary": oa_connection(":memory:")},
            databases={"colocated": CDMDatabaseConfig(connection="primary")},
        )
        resolved = Resolver(stack).resolve_database("colocated")
        _metadata, concept, person, cond = _cdm_tables()
        *_, derived = without_cross_engine_foreign_keys(
            (concept, person, cond), resolved=resolved
        )
        dialect = postgresql.dialect()
        assert str(CreateTable(derived).compile(dialect=dialect)).count("FOREIGN KEY") == 2

    def test_the_original_table_is_never_mutated(self, compile_split_cdm):
        _, _, _, cond = _cdm_tables()
        before = str(CreateTable(cond).compile(dialect=postgresql.dialect()))
        without_cross_engine_foreign_keys((cond,), resolved=compile_split_cdm)
        assert str(CreateTable(cond).compile(dialect=postgresql.dialect())) == before


class TestBoundaryGuard:
    pytestmark: ClassVar[list[pytest.MarkDecorator]] = [
        pytest.mark.postgresql,
        pytest.mark.db_dialect,
    ]

    def test_cross_tag_statement_raises_before_execution(self, split_cdm):
        _, concept, _, cond = _cdm_tables()
        primary, vocab = split_cdm.create_engines()
        try:
            with primary.connect() as connection, pytest.raises(
                CrossDatabaseStatementError, match="'vocab'.*'primary'"
            ):
                connection.execute(
                    sa.select(cond.c.id).join(
                        concept, cond.c.concept_id == concept.c.concept_id
                    )
                )
            with vocab.connect() as connection, pytest.raises(
                CrossDatabaseStatementError, match="'primary'.*'vocab'"
            ):
                connection.execute(
                    sa.select(cond.c.id).join(
                        concept, cond.c.concept_id == concept.c.concept_id
                    )
                )
        finally:
            primary.dispose()
            vocab.dispose()

    def test_wrong_engine_raises_instead_of_reading_a_shadow_copy(self, split_cdm):
        """A stale copy of a vocabulary table on the primary database would
        satisfy a vocab-only statement sent to the primary engine. The guard
        refuses it, while the vocab engine reads the real table."""
        _metadata, concept, _, _ = _cdm_tables()
        primary, vocab = split_cdm.create_engines()
        try:
            for engine in (primary, vocab):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text("CREATE TABLE concept (concept_id INTEGER PRIMARY KEY)")
                    )
            with primary.connect() as connection, pytest.raises(
                CrossDatabaseStatementError, match="'vocab'"
            ):
                connection.execute(sa.select(concept.c.concept_id))
            with vocab.connect() as connection:
                assert connection.execute(sa.select(concept.c.concept_id)).all() == []
        finally:
            for engine in (primary, vocab):
                with engine.begin() as connection:
                    connection.execute(sa.text("DROP TABLE IF EXISTS concept"))
                engine.dispose()

    def test_caller_claimed_tag_is_hosted_by_the_engine_that_claimed_it(self, split_cdm):
        """A caller tag such as a staging schema is local to whichever engine
        claimed it, so combining it with vocab on the vocab engine is allowed."""
        _, concept, _, _ = _cdm_tables()
        staging = sa.Table(
            "staging_concept", sa.MetaData(),
            sa.Column("concept_id", sa.Integer), schema="staging",
        )
        primary, vocab = split_cdm.create_engines(
            schema_claims=[SchemaClaim(schema_tag="staging", physical_schema="public")],
            register_claims=False,
        )
        try:
            with vocab.begin() as connection:
                connection.execute(
                    sa.text("CREATE TABLE concept (concept_id INTEGER PRIMARY KEY)")
                )
                connection.execute(sa.text("CREATE TABLE staging_concept (concept_id INTEGER)"))
                assert connection.execute(
                    sa.select(concept.c.concept_id).join(
                        staging, staging.c.concept_id == concept.c.concept_id
                    )
                ).all() == []
                connection.execute(sa.text("DROP TABLE staging_concept"))
                connection.execute(sa.text("DROP TABLE concept"))
        finally:
            primary.dispose()
            vocab.dispose()

    def test_single_tag_statements_are_untouched_on_both_engines(self, split_cdm):
        primary, vocab = split_cdm.create_engines()
        try:
            for engine in (primary, vocab):
                with engine.connect() as connection:
                    assert connection.execute(sa.select(sa.literal(1))).scalar() == 1
        finally:
            primary.dispose()
            vocab.dispose()

    def test_raw_text_sql_is_not_guarded(self, split_cdm):
        """A documented limit, asserted so it cannot be mistaken for covered:
        text() carries no table metadata, so the guard cannot see its tags."""
        primary, vocab = split_cdm.create_engines()
        try:
            with primary.connect() as connection:
                assert connection.execute(sa.text("SELECT 1")).scalar() == 1
        finally:
            primary.dispose()
            vocab.dispose()

    def test_no_guard_is_installed_on_a_colocated_database(
        self, pg_db, pg_connection_config, cleanup_after_test
    ):
        """One database hosts every tag, so a cross-tag statement is ordinary
        SQL and must not be refused."""
        name = f"colocated_guard_{uuid.uuid4().hex[:8]}"
        stack = StackConfig.for_session(
            connections={"primary": pg_connection_config},
            databases={name: GenericDatabaseConfig(connection="primary")},
        )
        resolved = Resolver(stack).resolve_database(name)
        engine = resolved.create_engine()
        try:
            with engine.connect() as connection:
                assert connection.execute(sa.select(sa.literal(1))).scalar() == 1
        finally:
            engine.dispose()
