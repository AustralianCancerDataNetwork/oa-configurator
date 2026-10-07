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
            OmopAlchemyConfig, "test_cdm_db_sqlite", dialect=Dialect.SQLITE,
        ) as db:
            yield db.connection.engine

For code under test that needs a real, genuinely-committing ``Engine``
(``.connect()``/``.begin()`` repeatedly, which a rolled-back ``Connection``
can't stand in for), use ``db.committing_engine``. It is outside the rollback
transaction, so register cleanup with ``cleanup_after_test``::

    @pytest.fixture
    def pg_engine(pg_db):
        return pg_db.committing_engine

Every supported dialect gets its own pytest marker, plus ``db_dialect`` on
whichever ones are capable of corrupting shared ORM metadata across
dialects in one process (see ``_can_corrupt_shared_metadata`` for the
mechanism). A consuming repo's ``addopts = "-m 'not db_dialect'"`` excludes
those by default; ``pytest -m <dialect>`` runs just one. Parametrize a
fixture across dialects with ``DIALECT_PARAMS`` since each param already 
carries the right marks. It also auto-applies to any test whose fixture 
closure includes ``pg_db``.

That auto-detection is static and name-based, so it silently misses a
fixture that doesn't follow the ``pg_db`` convention. Pass your fixture's
own ``request`` (``isolated_test_database(..., request=request)``) to
close that gap: it raises at fixture-setup time, before any DDL runs, if
the resolved dialect needs ``db_dialect`` but the test isn't marked.
Recommended for every fixture not already covered by ``DIALECT_PARAMS``.

A test that must hold a real, committing ``Engine``/``Connection`` gets no
automatic cleanup from ``isolated_test_database()``, since rollback only
isolates code operating on the connection it's handed. Use the
``cleanup_after_test`` fixture to register whatever the test needs undone at
teardown.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any, Iterable, NamedTuple, cast

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import registry

from ..domains.resources.sql import Dialect, Role
from .base import (
    IsolatedTestDatabase,
    TestDatabaseNotConfigured,
    TestDatabaseStrategy,
    _skip_message,
)
from .postgres import PostgresTestStrategy, drop_schema_if_exists, install_postgres_extension
from .sqlite import SQLiteTestStrategy

if TYPE_CHECKING:
    from ..domains.resources.schema import ResolvedDatabase, SchemaClaim
    from ..package_base import PackageConfigBase
    from ..resolver import Resolver

__all__ = [
    "DIALECT_PARAMS",
    "IsolatedTestDatabase",
    "ScopedTestSchema",
    "cleanup_after_test",
    "delete_rows_on_cleanup",
    "drop_schema_if_exists",
    "guarded_resolver",
    "install_postgres_extension",
    "isolated_test_database",
    "isolated_test_schema",
    "reset_schema_registry_rows",
    "resolve_with_role_schemas",
    "scoped_test_schema",
]

def pytest_configure(config: pytest.Config) -> None:
    # See _can_corrupt_shared_metadata() for what "corrupting" means here.
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
    ``DIALECT_PARAMS`` uses instead of a second, independent guess. This
    detection can still miss a renamed or unconventional fixture silently
    -- see ``_require_db_dialect_mark`` for the actual backstop.
    """
    for item in items:
        if "pg_db" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.postgresql)
            if _can_corrupt_shared_metadata(Dialect.POSTGRESQL):
                item.add_marker(pytest.mark.db_dialect)


_STRATEGIES: dict[Dialect, type[TestDatabaseStrategy]] = {
    Dialect.POSTGRESQL: PostgresTestStrategy,
    Dialect.SQLITE: SQLiteTestStrategy,
}


def _can_corrupt_shared_metadata(dialect: str) -> bool:
    """Whether *dialect*'s create_all() can defer a constraint via ALTER,
    the one mechanism that mutates shared metadata state process-wide.

    SQLAlchemy's AddConstraint permanently mutates a ForeignKeyConstraint
    object the first time it defers a circular-dependency FK on an
    ALTER-capable dialect, corrupting later create_all() calls against the
    same shared Base.metadata on any other dialect in the same process. A
    dialect that can't ALTER (e.g. SQLite) can never trigger this itself.

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
        dialect = Dialect(dialect_name)
    except ValueError:
        raise NotImplementedError(
            f"No test-database strategy registered for dialect {dialect_name!r}. "
            f"Supported: {sorted(d.value for d in _STRATEGIES)}."
        ) from None
    return _STRATEGIES[dialect]()


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
    dialect: Dialect | str | None = None,
    request: pytest.FixtureRequest | None = None,
    resolver: Resolver | None = None,
    schema_claims: Iterable["SchemaClaim"] = (),
    execution_options: dict[str, Any] | None = None,
    **engine_kwargs: Any,
) -> Generator[IsolatedTestDatabase, None, None]:
    """Resolve *field_name* off *config_cls* and yield an isolated test database.

    The one thing every repo's ``conftest.py`` should call. ``test_only``-checked,
    dialect-dispatched: see module docstring.

    Parameters
    ----------
    resolver : Resolver, optional
        Resolve against this ``Resolver`` instead of the on-disk active
        config, so a consumer can inject a session-built ``StackConfig``.
    dialect : Dialect or str, optional
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
    engine_kwargs
        Forwarded to the underlying ``ResolvedDatabase.create_engine()``
        call, e.g. ``poolclass``/``connect_args`` for a caller that needs
        to tune the engine (a session-scoped SQLite engine that must share
        one real connection via ``poolclass=StaticPool``, for example), or
        ``extensions`` for a connect-event callable the engine needs on
        every physical connection (``install_postgres_extension()`` builds
        one for a named Postgres extension).
    """
    if dialect is not None and dialect not in _STRATEGIES:
        raise ValueError(f"Unknown dialect {dialect!r}. Registered: {sorted(d.value for d in _STRATEGIES)}.")

    try:
        resolved: "ResolvedDatabase" = TestDatabaseStrategy._resolve_and_check(
            config_cls, field_name, resolver=resolver
        )
    except TestDatabaseNotConfigured as exc:
        if dialect is None:
            pytest.skip(_skip_message(exc.field_name or field_name))
        try:
            resolved = _STRATEGIES[Dialect(dialect)]().resolve_without_config()
        except TestDatabaseNotConfigured:
            pytest.skip(_skip_message(exc.field_name or field_name))

    resolved_dialect_name = sa.engine.make_url(resolved.connection.url).get_backend_name()
    if dialect is not None and resolved_dialect_name != dialect:
        expected = dialect.value if isinstance(dialect, Dialect) else dialect
        raise ValueError(
            f"{field_name!r} resolves to dialect {resolved_dialect_name!r}, expected {expected!r}."
        )

    if request is not None:
        _require_db_dialect_mark(request, field_name, resolved_dialect_name)

    strategy = _strategy_for(resolved_dialect_name)
    with strategy.isolated_database(
        resolved,
        schema_claims=schema_claims,
        execution_options=execution_options,
        **engine_kwargs,
    ) as db:
        yield db


@contextmanager
def isolated_test_schema(engine: sa.Engine, *, prefix: str = "test") -> Generator[str, None, None]:
    """Yield a uniquely-named, genuinely-committed schema, dropped on exit.

    The narrow exception path for code that constructs its own engine and
    must see real, committed state. See :func:`isolated_test_database` for
    the default (and much more common) rollback-based path.
    """
    strategy = _strategy_for(engine.dialect.name)
    with strategy.temporary_schema(engine, prefix=prefix) as schema:
        yield schema


class ScopedTestSchema(NamedTuple):
    """A resolved database re-pointed at fresh test schemas, with its engine.

    Attributes
    ----------
    resolved : ResolvedDatabase
        Copy of the input resolved with every role pointed at its test schema.
    engine : sqlalchemy.engine.Engine
        ``resolved.create_engine()`` for the primary role.
    schemas : dict[Role, str]
        Physical test schema per role.
    """

    resolved: ResolvedDatabase
    engine: sa.Engine
    schemas: dict[Role, str]


def guarded_resolver(resolved: ResolvedDatabase, *, resolver: Resolver | None = None) -> Resolver:
    """Resolver in which every connection of *resolved* has ``test_only=False``, so the provenance guard runs.

    The connections are overridden in memory under their own names, so
    ``resolver.resolve_database(resolved.name)`` resolves the same entry.

    Parameters
    ----------
    resolved : ResolvedDatabase
        Database whose connections are overridden.
    resolver : Resolver, optional
        Resolver holding the entry. Defaults to ``Resolver.from_active_config()``.

    Returns
    -------
    Resolver
    """
    from ..resolver import Resolver

    resolver = resolver if resolver is not None else Resolver.from_active_config()
    names = {
        resolved.connection_for_role(resolved.route_for_schema_tag(tag, vocab=Role.VOCAB, primary=Role.PRIMARY)).name
        for tag in resolved.schema_tags()
    }
    return resolver.with_overrides(
        connections={name: resolver.config.connections[name].model_copy(update={"test_only": False}) for name in names}
    )


def resolve_with_role_schemas(
    resolved: ResolvedDatabase,
    schemas: Mapping[Role, str | None],
    *,
    resolver: Resolver | None = None,
) -> ResolvedDatabase:
    """Resolve *resolved*'s config entry again with each role in *schemas* pointed at its given schema.

    The entry is overridden in memory through ``Resolver.with_overrides()``, so
    the regular resolution applies, including role fallbacks.

    Parameters
    ----------
    resolved : ResolvedDatabase
        Database whose ``[databases.*]`` entry is overridden.
    schemas : Mapping[Role, str or None]
        Schema per role. Unlisted roles keep their configured value.
    resolver : Resolver, optional
        Resolver holding the entry. Defaults to ``Resolver.from_active_config()``.

    Returns
    -------
    ResolvedDatabase

    Raises
    ------
    ValueError
        If *schemas* names a role the entry has no schema field for.
    """
    from ..domains.resources.schema import _iter_schema_roles
    from ..resolver import Resolver

    resolver = resolver if resolver is not None else Resolver.from_active_config()
    entry = resolver.config.databases[resolved.name]
    field_by_role = {role: name for name, role in _iter_schema_roles(type(entry))}
    unknown = sorted(role.name for role in schemas if role not in field_by_role)
    if unknown:
        raise ValueError(f"Role(s) {unknown} have no schema field on {type(entry).__name__}.")
    overridden = type(entry).model_validate(
        {**entry.model_dump(), **{field_by_role[role]: schema for role, schema in schemas.items()}}
    )
    return resolver.with_overrides(databases={resolved.name: overridden}).resolve_database(resolved.name)


@contextmanager
def scoped_test_schema(
    resolved: ResolvedDatabase,
    *,
    prefix: str = "test",
    split_roles: Iterable[Role] = (),
    resolver: Resolver | None = None,
    schema_claims: Iterable[SchemaClaim] = (),
    extensions: Sequence[Callable[[Any, Any], None]] = (),
    owner: str | None = None,
    **engine_kwargs: Any,
) -> Generator[ScopedTestSchema, None, None]:
    """Point every role of *resolved* at fresh committed schemas and yield it with its engine.

    Roles sharing a connection share one schema, except roles in
    *split_roles*, which each get their own. Schemas are created on the
    role's own connection and dropped on exit. The config entry name is
    kept. Postgres only.

    Parameters
    ----------
    resolved : ResolvedDatabase
        Database to scope. Its connections must be ``test_only``.
    prefix : str, optional
        Prefix for the generated schema names.
    split_roles : Iterable[Role], optional
        Roles that get a schema of their own.
    resolver : Resolver, optional
        Resolver holding *resolved*'s entry. Defaults to ``Resolver.from_active_config()``.
    schema_claims, extensions, owner, **engine_kwargs
        Forwarded to ``ResolvedDatabase.create_engine()``.

    Yields
    ------
    ScopedTestSchema
    """
    split = set(split_roles)
    with ExitStack() as stack:
        ddl_engines: dict[str, sa.Engine] = {}
        shared_schemas: dict[str, str] = {}
        schemas: dict[Role, str] = {}
        for role in resolved.schema_tags():
            connection_role = resolved.route_for_schema_tag(role, vocab=Role.VOCAB, primary=Role.PRIMARY)
            connection = resolved.connection_for_role(connection_role)
            if connection.name not in ddl_engines:
                ddl_engines[connection.name] = connection.create_engine()
                stack.callback(ddl_engines[connection.name].dispose)
            ddl_engine = ddl_engines[connection.name]
            if role in split:
                schemas[role] = stack.enter_context(
                    isolated_test_schema(ddl_engine, prefix=f"{prefix}_{role.value}")
                )
                continue
            if connection.name not in shared_schemas:
                shared_schemas[connection.name] = stack.enter_context(
                    isolated_test_schema(ddl_engine, prefix=prefix)
                )
            schemas[role] = shared_schemas[connection.name]

        scoped = resolve_with_role_schemas(resolved, schemas, resolver=resolver)
        engine = scoped.create_engine(
            schema_claims=schema_claims, extensions=extensions, owner=owner, **engine_kwargs
        )
        stack.callback(engine.dispose)
        yield ScopedTestSchema(resolved=scoped, engine=engine, schemas=schemas)


@pytest.fixture
def cleanup_after_test() -> Iterator[Callable[[Callable[[], None]], None]]:
    """Register teardown work to run unconditionally after this test.

    ``isolated_test_database()`` already gives every test the guarantee
    that it leaves nothing behind, via rollback (Postgres) or a vanishing
    tempfile (SQLite). This fixture is the counterpart for a test that
    genuinely can't get that guarantee for free because it holds a real,
    committing engine or connection instead (``pg_engine``,
    ``resolved.create_engine()``, ``target_connection.create_engine()``).
    Register whatever undoes what the test actually committed::

        def test_writes_a_row(pg_engine, cleanup_after_test):
            with pg_engine.begin() as conn:
                conn.execute(my_table.insert().values(id=1))
            cleanup_after_test(lambda: _delete_row(pg_engine, 1))

    Callbacks run in reverse-registration order, even when the test itself
    raises. One callback raising doesn't stop the rest from running; every
    exception raised by a callback is collected and re-raised together
    once all of them have run.

    Yields
    ------
    Callable[[Callable[[], None]], None]
        Call this with a zero-argument callback to register it.
    """
    callbacks: list[Callable[[], None]] = []
    try:
        yield callbacks.append
    finally:
        errors: list[Exception] = []
        for callback in reversed(callbacks):
            try:
                callback()
            except Exception as exc:  # noqa: BLE001 - collected, not swallowed
                errors.append(exc)
        if errors:
            raise ExceptionGroup("cleanup_after_test callback(s) failed", errors)


def delete_rows_on_cleanup(
    cleanup_after_test: Callable[[Callable[[], None]], None],
    engine: sa.Engine,
    table: sa.Table,
    whereclause: Any,
) -> None:
    """Register the common row-deletion cleanup case with :func:`cleanup_after_test`.

    Equivalent to calling ``cleanup_after_test`` with a callback that opens
    a fresh connection on *engine* and deletes the matching rows. A fresh
    connection is used because the test's own connection may already be
    closed by the time teardown runs.

    Parameters
    ----------
    cleanup_after_test : Callable[[Callable[[], None]], None]
        The registration function yielded by the ``cleanup_after_test`` fixture.
    engine : sqlalchemy.engine.Engine
        Opened fresh at teardown time to run the delete.
    table : sqlalchemy.Table
        Table to delete rows from.
    whereclause : Any
        Passed to ``table.delete().where(...)``.
    """

    def _cleanup() -> None:
        with engine.begin() as conn:
            conn.execute(table.delete().where(whereclause))

    cleanup_after_test(_cleanup)


def reset_schema_registry_rows(
    cleanup_after_test: Callable[[Callable[[], None]], None],
    engine: sa.Engine,
    schema_tags: Iterable[str] = tuple(role.value for role in Role),
) -> None:
    """Remove the schema_registry rows for *schema_tags* on *engine*'s database for this test and restore them afterwards.

    For tests that build non-``test_only`` engines on a shared test database
    and need those tags without a baseline, while leaving the database's own
    baselines intact for later tests.

    Parameters
    ----------
    cleanup_after_test : Callable[[Callable[[], None]], None]
        The registration function yielded by the ``cleanup_after_test`` fixture.
    engine : sqlalchemy.engine.Engine
        Its URL selects the physical database.
    schema_tags : Iterable[str], optional
        Tags whose rows are removed and restored. Defaults to the Role tags.

    Raises
    ------
    pytest.fail.Exception
        If *engine*'s physical database doesn't match a known
        ``test_only=true`` connection in the active config, regardless of
        whether the *claim*/connection this engine was built with was itself
        marked ``test_only``. This function mutates real registry rows, so the
        physical database it runs against must be a known test server even
        when exercising non-``test_only`` claim behavior against it.
    """
    from ..domains.resources.schema_registry import SchemaRegistry, _registry_connection
    from ..domains.resources.sql import connection_key

    TestDatabaseStrategy._require_test_only_engine(engine)

    table = cast(sa.Table, SchemaRegistry.__table__)
    condition = sa.and_(
        table.c.connection_key == connection_key(engine.url),
        table.c.schema_tag.in_(list(schema_tags)),
    )

    with engine.begin() as connection:
        registry_connection = _registry_connection(connection)
        saved = []
        if registry_connection is not None:
            saved = [
                {key: value for key, value in row._mapping.items() if key != "id"}
                for row in registry_connection.execute(sa.select(table).where(condition))
            ]
            registry_connection.execute(table.delete().where(condition))

    def _restore() -> None:
        with engine.begin() as connection:
            registry_connection = _registry_connection(connection)
            if registry_connection is None:
                return
            registry_connection.execute(table.delete().where(condition))
            if saved:
                registry_connection.execute(sa.insert(table), saved)

    cleanup_after_test(_restore)
