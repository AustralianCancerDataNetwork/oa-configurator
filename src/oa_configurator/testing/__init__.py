"""Isolated test-database provisioning: one mechanism for every dialect.

The public surface every repo's ``conftest.py`` needs::

    from oa_configurator.testing import isolated_test_database

    @pytest.fixture
    def pg_db(request):
        with isolated_test_database(OmopAlchemyConfig, "test_cdm_db") as db:
            yield db

Dialect is resolved automatically from the target connection. Postgres gets
rollback-based isolation (nothing ever commits, so concurrent test runs
against the same shared server structurally cannot collide); SQLite gets a
fresh, disposable database per call (already free, no special-casing
needed). Add a new dialect by subclassing
:class:`~oa_configurator.testing.base.TestDatabaseStrategy` and registering
it in ``_STRATEGIES`` below, nothing else here changes.

Also registers this package as the ``pytest11`` plugin entry point (see
``pyproject.toml``).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

import sqlalchemy as sa

from .base import IsolatedTestDatabase, TestDatabaseStrategy
from .postgres import PostgresTestStrategy
from .sqlite import SQLiteTestStrategy

if TYPE_CHECKING:
    from ..package_base import PackageConfigBase

__all__ = [
    "IsolatedTestDatabase",
    "isolated_test_database",
    "isolated_test_schema",
]

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
    extensions: Sequence[str] = (),
    **engine_kwargs: object,
) -> Iterator[IsolatedTestDatabase]:
    """Resolve *field_name* off *config_cls* and yield an isolated test database.

    The one thing every repo's ``conftest.py`` should call. ``test_only``-checked,
    dialect-dispatched: see module docstring.
    """
    resolved = TestDatabaseStrategy._resolve_and_check(config_cls, field_name)
    dialect_name = sa.engine.make_url(resolved.connection.url).get_backend_name()
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
