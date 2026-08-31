"""Isolated test-database provisioning: one mechanism for every dialect.

The only function a consuming repo's conftest.py needs; no strategy class
is ever imported directly::

    from oa_configurator.testing import isolated_test_database

    @pytest.fixture
    def pg_db():
        with isolated_test_database(OmopAlchemyConfig, "test_cdm_db_pg") as db:
            yield db

Dialect is resolved from the target connection: Postgres gets rollback-based
isolation, SQLite gets a fresh disposable database per call. Pass
``dialect=`` to pin one dialect regardless of what the field resolves to
(mismatch always raises; unconfigured falls back to that dialect's own
``resolve_without_config()`` if supported, else skips)::

    @pytest.fixture
    def empty_engine():
        with isolated_test_database(
            OmopAlchemyConfig, "test_cdm_db_sqlite", dialect="sqlite",
        ) as db:
            yield db.connection.engine

For code under test that needs a real, genuinely-committing ``Engine``
(``.connect()``/``.begin()`` repeatedly, which a rolled-back ``Connection``
can't stand in for), use ``db.connection.engine`` directly::

    @pytest.fixture
    def pg_engine(pg_db):
        return pg_db.connection.engine

Also registers the ``requires_process_isolation`` pytest marker and applies
it to any test whose fixture closure includes ``pg_db`` (see
``pytest_configure`` below for why).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from .base import (
    ConfigurationError,
    IsolatedTestDatabase,
    TestDatabaseStrategy,
    _skip_message,
)
from .postgres import PostgresTestStrategy
from .sqlite import SQLiteTestStrategy

if TYPE_CHECKING:
    from ..domains.resources.schema import ResolvedDatabase
    from ..package_base import PackageConfigBase

__all__ = [
    "IsolatedTestDatabase",
    "isolated_test_database",
    "isolated_test_schema",
]


def pytest_configure(config: pytest.Config) -> None:
    # Why this marker exists: SQLAlchemy's AddConstraint permanently mutates
    # a ForeignKeyConstraint object the first time it defers a
    # circular-dependency FK on an ALTER-capable dialect (Postgres), which
    # corrupts later create_all() calls against the same shared
    # Base.metadata on a non-ALTER-capable dialect (SQLite) in the same
    # process. Never mixing the two dialects in one process is the only fix
    # that doesn't depend on private SQLAlchemy internals.
    config.addinivalue_line(
        "markers",
        "requires_process_isolation: must not run in the same process as "
        "other dialects' tests (see oa_configurator.testing's module "
        "docstring for why). Excluded by an "
        "`addopts = \"-m 'not requires_process_isolation'\"` in a consuming "
        "repo's pyproject.toml; run explicitly via "
        "`pytest -m requires_process_isolation`.",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark any test whose fixture closure includes ``pg_db``.

    ``item.fixturenames`` is the full transitive fixture closure, so a test
    requesting ``pg_session`` (which depends on ``pg_engine``, which depends
    on ``pg_db``) still has ``pg_db`` in it. No repo needs to mark its own
    tests, as long as its real-database fixture follows the established
    ``pg_db`` name every consumer already uses.
    """
    for item in items:
        if "pg_db" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.requires_process_isolation)


_STRATEGIES: dict[str, type[TestDatabaseStrategy]] = {
    "postgresql": PostgresTestStrategy,
    "sqlite": SQLiteTestStrategy,
}


def _strategy_for(dialect_name: str) -> TestDatabaseStrategy:
    try:
        return _STRATEGIES[dialect_name]()
    except KeyError:
        raise NotImplementedError(
            f"No test-database strategy registered for dialect {dialect_name!r}. "
            f"Supported: {sorted(_STRATEGIES)}."
        ) from None


@contextmanager
def isolated_test_database(
    config_cls: type["PackageConfigBase"],
    field_name: str,
    *,
    dialect: str | None = None,
    extensions: Sequence[str] = (),
    **engine_kwargs: object,
) -> Iterator[IsolatedTestDatabase]:
    """Resolve *field_name* off *config_cls* and yield an isolated test database.

    The one thing every repo's ``conftest.py`` should call. ``test_only``-checked,
    dialect-dispatched: see module docstring.

    Parameters
    ----------
    dialect : str, optional
        Assert the resolved connection is actually this dialect, raising
        if not. A field configured for the wrong dialect is a bug, never
        silently substituted. If *field_name* isn't configured at all and
        *dialect* names a strategy whose ``resolve_without_config()``
        succeeds, that's used instead of skipping.
    """
    if dialect is not None and dialect not in _STRATEGIES:
        raise ValueError(f"Unknown dialect {dialect!r}. Registered: {sorted(_STRATEGIES)}.")

    try:
        resolved: "ResolvedDatabase" = TestDatabaseStrategy._resolve_and_check(config_cls, field_name)
    except ConfigurationError as exc:
        if dialect is None:
            pytest.skip(_skip_message(exc.field_name or field_name))
        try:
            resolved = _STRATEGIES[dialect]().resolve_without_config()
        except ConfigurationError:
            pytest.skip(_skip_message(exc.field_name or field_name))

    dialect_name = sa.engine.make_url(resolved.connection.url).get_backend_name()
    if dialect is not None and dialect_name != dialect:
        raise ValueError(
            f"{field_name!r} resolves to dialect {dialect_name!r}, expected {dialect!r}."
        )

    strategy = _strategy_for(dialect_name)
    with strategy.isolated_database(resolved, extensions=extensions, **engine_kwargs) as db:
        yield db


@contextmanager
def isolated_test_schema(engine: sa.Engine, *, prefix: str = "test") -> Iterator[str]:
    """Yield a uniquely-named, genuinely-committed schema, dropped on exit.

    The narrow exception path for code that constructs its own engine and
    must see real, committed state. See :func:`isolated_test_database` for
    the default (and much more common) rollback-based path.
    """
    strategy = _strategy_for(engine.dialect.name)
    with strategy.temporary_schema(engine, prefix=prefix) as schema:
        yield schema
