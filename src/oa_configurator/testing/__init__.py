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

Registers one pytest marker per supported dialect (``sqlite``,
``postgresql``, ...), and additionally marks each one ``db_dialect`` if
(and only if) it's capable of causing the corruption described below. A
consuming repo's ``addopts = "-m 'not db_dialect'"`` then excludes exactly
those tests by default, without hardcoding which dialect that is or
enumerating dialects by name. A future dialect gets the right treatment
automatically, whichever way its own capability actually falls.
``pytest -m <dialect>`` runs just that one dialect. A fixture
parametrized across dialects should use ``DIALECT_PARAMS`` with
``dialect=request.param`` rather than a hand-rolled params list, since
each param already carries the right marks (see ``pytest_configure``
below for why mixing dialects in one process is unsafe at all). Also
auto-applies ``postgresql`` + ``db_dialect`` to any test whose fixture
closure includes ``pg_db``.

That auto-detection is static and name-based, so it can miss a fixture
that doesn't follow the ``pg_db`` convention -- silently, with no error.
Pass this function your fixture's own ``request`` (``isolated_test_database(
..., request=request)``) to close that gap: it raises at fixture-setup
time, before any DDL runs, if the resolved dialect can corrupt shared
metadata but the test isn't marked ``db_dialect``. Recommended for every
fixture not already covered by ``DIALECT_PARAMS``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import registry

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
    "DIALECT_PARAMS",
    "IsolatedTestDatabase",
    "isolated_test_database",
    "isolated_test_schema",
]

def pytest_configure(config: pytest.Config) -> None:
    # SQLAlchemy's AddConstraint permanently mutates a ForeignKeyConstraint 
    # object the first time it defers a circular-dependency FK on an 
    # ALTER-capable dialect, which  corrupts later create_all() calls 
    # against the same shared Base.metadata on any other dialect 
    # in the same process. A dialect that can't ALTER (e.g. SQLite) 
    # can never trigger this itself, so it doesn't need excluding. 
    # A consuming repo's `addopts = "-m 'not db_dialect'"` keeps every 
    # dialect capable of the corruption out of the default run.
    # `pytest -m <dialect>` is the explicit way to run a single dialect.
    for dialect in _STRATEGIES:
        config.addinivalue_line("markers", f"{dialect}: exercises the {dialect} dialect")
    config.addinivalue_line(
        "markers", "db_dialect: exercises a dialect capable of corrupting shared metadata"
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-mark any test whose fixture closure includes ``pg_db``.

    ``item.fixturenames`` is the full transitive fixture closure, so a test
    requesting ``pg_session`` (which depends on ``pg_engine``, which depends
    on ``pg_db``) still has ``pg_db`` in it. No repo needs to mark its own
    tests, as long as its real-database fixture follows the established
    ``pg_db`` name every consumer already uses. A dialect-parametrized
    fixture that resolves ``pg_db`` dynamically via
    ``request.getfixturevalue`` isn't visible here. Use ``DIALECT_PARAMS``
    for those, which marks each param directly instead of relying on
    static detection.

    ``db_dialect`` is derived from ``_can_corrupt_shared_metadata`` rather
    than applied unconditionally, so this stays the same source of truth
    ``DIALECT_PARAMS`` uses instead of a second, independent guess. Since
    this static, name-based detection can miss a renamed or unconventional
    fixture entirely (with no error, just silent under-marking),
    ``isolated_test_database``'s own ``request=`` guard is the real backstop:
    it raises at fixture-setup time, before any DDL runs, if a
    corruption-capable dialect resolves without this mark present.
    """
    for item in items:
        if "pg_db" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.postgresql)
            if _can_corrupt_shared_metadata("postgresql"):
                item.add_marker(pytest.mark.db_dialect)


_STRATEGIES: dict[str, type[TestDatabaseStrategy]] = {
    "postgresql": PostgresTestStrategy,
    "sqlite": SQLiteTestStrategy,
}


def _can_corrupt_shared_metadata(dialect: str) -> bool:
    """Whether *dialect*'s create_all() can defer a constraint via ALTER,
    the one mechanism that mutates shared metadata state process-wide.

    Loads the dialect class through SQLAlchemy's own plugin registry
    rather than ``create_engine()``, so this needs no driver installed and
    no real connection, since ``supports_alter`` is a database-level
    capability, identical across every driver for the same dialect.
    """
    return bool(registry.load(dialect).supports_alter)


DIALECT_PARAMS = tuple(
    pytest.param(
        dialect,
        marks=(
            (getattr(pytest.mark, dialect), pytest.mark.db_dialect)
            if _can_corrupt_shared_metadata(dialect)
            else getattr(pytest.mark, dialect)
        ),
    )
    for dialect in _STRATEGIES
)


def _strategy_for(dialect_name: str) -> TestDatabaseStrategy:
    try:
        return _STRATEGIES[dialect_name]()
    except KeyError:
        raise NotImplementedError(
            f"No test-database strategy registered for dialect {dialect_name!r}. "
            f"Supported: {sorted(_STRATEGIES)}."
        ) from None


def _require_db_dialect_mark(request: pytest.FixtureRequest, field_name: str, dialect_name: str) -> None:
    """Raise if *request*'s test can corrupt shared metadata but isn't marked for it.

    ``pytest_collection_modifyitems``'s ``pg_db``-name detection is static
    and can miss a renamed or unconventionally-named fixture with no error
    at all -- just silent under-marking, letting a corruption-capable
    dialect run in the default suite alongside SQLite. This is the actual
    backstop: it runs at fixture-setup time, with the real resolved
    dialect in hand, before any DDL executes, so a drifted naming
    convention fails loudly here instead of causing an intermittent,
    unrelated failure in some other test later in the same process.
    """
    if not _can_corrupt_shared_metadata(dialect_name):
        return
    if request.node.get_closest_marker("db_dialect") is not None:
        return
    raise RuntimeError(
        f"{field_name!r} resolved to dialect {dialect_name!r}, which can corrupt shared "
        "ORM metadata if it shares a process with another dialect, but this test isn't "
        "marked db_dialect. If this fixture doesn't match a known auto-detected naming "
        "convention (e.g. 'pg_db'), mark the test explicitly with pytest.mark.db_dialect, "
        "or parametrize it with DIALECT_PARAMS instead."
    )


@contextmanager
def isolated_test_database(
    config_cls: type["PackageConfigBase"],
    field_name: str,
    *,
    dialect: str | None = None,
    extensions: Sequence[str] = (),
    request: pytest.FixtureRequest | None = None,
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
    request : pytest.FixtureRequest, optional
        Pass the calling fixture's own ``request`` to enable a safety
        check: if the resolved dialect can corrupt shared ORM metadata
        (see ``pytest_configure``) but the current test isn't marked
        ``db_dialect``, raise immediately rather than silently letting it
        run unmarked in the default suite. Strongly recommended for any
        fixture not already covered by ``DIALECT_PARAMS`` (whose marks are
        always correct by construction).
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

    if request is not None:
        _require_db_dialect_mark(request, field_name, dialect_name)

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
