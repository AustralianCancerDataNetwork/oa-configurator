"""DDL on one dialect must not change the DDL a later call renders for another.

SQLAlchemy defers a circular foreign key through ``AddConstraint`` on a dialect
that supports ``ALTER`` and records that on the constraint. Without a guard, a
later ``create_all()`` for SQLite leaves the foreign key out. ``oa_configurator.testing``
installs the guard for every test, so these tests run with it in place.
"""

from __future__ import annotations

import sqlalchemy as sa

from oa_configurator import Dialect, preserve_create_rules


def _circular_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table(
        "a",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("b_id", sa.Integer, sa.ForeignKey("b.id", name="fk_a_b")),
    )
    sa.Table(
        "b",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("a_id", sa.Integer, sa.ForeignKey("a.id", name="fk_b_a")),
    )
    return metadata


def _ddl(metadata: sa.MetaData, dialect: Dialect, method: str = "create_all") -> str:
    statements: list[str] = []
    engine = sa.create_mock_engine(
        f"{dialect}://",
        lambda sql, *_, **__: statements.append(str(sql.compile(dialect=engine.dialect))),
    )
    getattr(metadata, method)(engine, checkfirst=False)
    return "\n".join(statements)


def _unguarded(method):
    """The SQLAlchemy method as it is without the plugin's guard."""
    return getattr(method, "__wrapped__", method)


def test_postgresql_ddl_defers_the_circular_key_and_sqlite_keeps_both_inline():
    metadata = _circular_metadata()
    assert "ALTER TABLE" in _ddl(metadata, Dialect.POSTGRESQL)
    assert _ddl(metadata, Dialect.SQLITE).count("FOREIGN KEY") == 2


def test_without_the_guard_sqlite_loses_foreign_keys_after_postgresql_ddl(monkeypatch):
    """The premise of the guard. If SQLAlchemy stops doing this, the guard can go."""
    monkeypatch.setattr(sa.MetaData, "create_all", _unguarded(sa.MetaData.create_all))
    metadata = _circular_metadata()
    _ddl(metadata, Dialect.POSTGRESQL)
    assert _ddl(metadata, Dialect.SQLITE).count("FOREIGN KEY") < 2


def test_the_plugin_guard_keeps_sqlite_foreign_keys_after_postgresql_ddl():
    metadata = _circular_metadata()
    _ddl(metadata, Dialect.POSTGRESQL)
    assert _ddl(metadata, Dialect.SQLITE).count("FOREIGN KEY") == 2


def test_the_plugin_guard_covers_drop_all(monkeypatch):
    monkeypatch.setattr(sa.MetaData, "create_all", _unguarded(sa.MetaData.create_all))
    metadata = _circular_metadata()
    _ddl(metadata, Dialect.POSTGRESQL, "drop_all")
    assert _ddl(metadata, Dialect.SQLITE).count("FOREIGN KEY") == 2


def test_preserve_create_rules_restores_every_constraint():
    metadata = _circular_metadata()
    constraints = [c for table in metadata.tables.values() for c in table.constraints]
    before = [c._create_rule for c in constraints]
    with preserve_create_rules(metadata):
        for constraint in constraints:
            constraint._create_rule = lambda compiler: False
    assert [c._create_rule for c in constraints] == before
