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
it in ``_STRATEGIES`` below -- nothing else here changes.

Also registers the ``requires_database`` pytest marker and this package as
the ``pytest11`` plugin entry point (see ``pyproject.toml``).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from .base import IsolatedTestDatabase, TestDatabaseStrategy, _not_test_only_message, _skip_message
from .postgres import PostgresTestStrategy
from .sqlite import SQLiteTestStrategy

if TYPE_CHECKING:
    from ..package_base import PackageConfigBase

__all__ = [
    "IsolatedTestDatabase",
    "isolated_test_database",
    "isolated_test_schema",
    # Deprecated compatibility names -- see the section below.
    "resolve_test_database",
    "ensure_test_db_exists",
    "ensure_test_user_exists",
    "create_fresh_test_db",
    "drop_test_db",
    "require_pg_extension",
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
    must see real, committed state -- see :func:`isolated_test_database` for
    the default (and much more common) rollback-based path.
    """
    strategy = _strategy_for(engine.dialect.name)
    with strategy.temporary_schema(engine, prefix=prefix) as schema:
        yield schema


# ---------------------------------------------------------------------------
# Deprecated compatibility wrappers - Removed in breaking release
# ---------------------------------------------------------------------------
def resolve_test_database(config_cls: type["PackageConfigBase"], field_name: str) -> str:
    """Deprecated: use :func:`isolated_test_database` instead."""
    import warnings

    warnings.warn(
        "resolve_test_database() is deprecated; use isolated_test_database() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    resolved = TestDatabaseStrategy._resolve_and_check(config_cls, field_name)
    return resolved.connection.url


def ensure_test_db_exists(url: str | sa.URL) -> None:
    """Deprecated: isolated_test_database() provisions automatically."""
    import warnings

    warnings.warn(
        "ensure_test_db_exists() is deprecated; isolated_test_database() provisions automatically.",
        DeprecationWarning,
        stacklevel=2,
    )
    PostgresTestStrategy()._ensure_test_db_exists(url)  # noqa: SLF001


def ensure_test_user_exists(url: str | sa.URL) -> None:
    """Deprecated: isolated_test_database() provisions automatically."""
    import warnings

    warnings.warn(
        "ensure_test_user_exists() is deprecated; isolated_test_database() provisions automatically.",
        DeprecationWarning,
        stacklevel=2,
    )
    PostgresTestStrategy()._ensure_test_user_exists(url)  # noqa: SLF001


def create_fresh_test_db(url: str | sa.URL, *, extensions: Sequence[str] = ()) -> sa.URL:
    """Deprecated: use isolated_test_database(), which isolates without a
    destructive whole-database reset."""
    import warnings

    warnings.warn(
        "create_fresh_test_db() is deprecated; isolated_test_database() isolates "
        "without a destructive reset.",
        DeprecationWarning,
        stacklevel=2,
    )
    return PostgresTestStrategy()._create_fresh_test_db(url, extensions=extensions)  # noqa: SLF001


def drop_test_db(url: str | sa.URL) -> None:
    """Deprecated: isolated_test_database() needs no manual teardown."""
    import warnings

    warnings.warn(
        "drop_test_db() is deprecated; isolated_test_database() needs no manual teardown.",
        DeprecationWarning,
        stacklevel=2,
    )
    PostgresTestStrategy()._drop_test_db(url)  # noqa: SLF001


def require_pg_extension(url: str | sa.URL, extension: str) -> None:
    """Deprecated: pass extensions=(...) to isolated_test_database() instead."""
    import warnings

    warnings.warn(
        "require_pg_extension() is deprecated; pass extensions=(...) to "
        "isolated_test_database() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    PostgresTestStrategy()._require_pg_extension(url, extension)  # noqa: SLF001


# ---------------------------------------------------------------------------
# pytest plugin hooks (pytest11 entry point: oa_configurator.testing)
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_database(*args): deprecated, predates isolated_test_database(). "
        "Skip when a named OA_Configurator database is absent, fail if it resolves "
        "to a non-test_only connection. Accepts one or more database-name strings.",
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Deprecated: a marker's fixed database-name argument can't track what
    database a config field actually resolves to, so it silently stops
    matching the moment that field is pointed at a differently-named
    database. isolated_test_database(config_cls, field_name) resolves by
    field name directly and already skips/fails the same way on its own --
    every pg_db-style fixture gets this for free with no marker needed.
    """
    from ..resolver import Resolver

    for marker in item.iter_markers("requires_database"):
        import warnings

        warnings.warn(
            "@pytest.mark.requires_database is deprecated; isolated_test_database() "
            "already skips/fails the same way on its own, resolved by field name "
            "instead of a fixed database-name string.",
            DeprecationWarning,
            stacklevel=2,
        )
        for name in marker.args:
            try:
                resolver = Resolver.from_active_config()
                resolved = resolver.resolve_database(str(name))
            except Exception:
                pytest.skip(_skip_message(str(name)))
            connection_name = resolved.connection.name
            if not resolver.config.connections[connection_name].test_only:
                pytest.fail(_not_test_only_message(str(name), connection_name))
