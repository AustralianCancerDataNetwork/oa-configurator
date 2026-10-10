"""Cross-database boundary: the pre-execution guard and foreign-key derivation.

Both concern a split deployment, where two schema tags live on separate
physical databases. The guard tests use genuinely separate databases, since
the hazard it addresses is a statement that succeeds against a leftover copy
of the remote table rather than failing.
"""

from __future__ import annotations

import uuid
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
