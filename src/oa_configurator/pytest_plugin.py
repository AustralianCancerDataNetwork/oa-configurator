"""OA_Configurator pytest plugin — auto-loaded via the ``pytest11`` entry point.

Provides the ``requires_resource`` marker, the ``resolve_test_resource`` fixture
helper, and standalone PostgreSQL database lifecycle utilities:

- ``ensure_test_db_exists(url)`` — create the target database if absent
- ``create_fresh_test_db(url)`` — drop and recreate (used by omop-emb)
- ``drop_test_db(url)`` — terminate connections and drop the database

Accepts two forms for the resource argument to ``requires_resource``:

- A ``ResourceSpec`` instance — checks ``spec.semantic_name`` (preferred, e.g.
  ``@pytest.mark.requires_resource(OrmLoaderConfig.TEST_DB)``).
- A plain ``str`` — used as the resource name directly (last-resort fallback).

Note: passing a ``PackageConfigBase`` subclass directly as a marker argument does
NOT work in pytest ≥ 9 — pytest treats any class argument as a test class and
applies the mark to it rather than forwarding it as a marker arg.  Use the named
``ClassVar[ResourceSpec]`` attribute instead.
"""

from __future__ import annotations

import sqlalchemy as sa


def _maintenance_engine(url: sa.URL) -> sa.Engine:
    """Engine connected to the 'postgres' maintenance DB on the same host."""
    return sa.create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")


def ensure_test_db_exists(url: str | sa.URL) -> None:
    """Create the target database if it does not already exist.

    Connects to the ``postgres`` maintenance database (same host/credentials)
    so the target database can be created without touching anything else.
    Safe to call repeatedly — idempotent.
    """
    target = sa.engine.make_url(url)
    admin = _maintenance_engine(target)
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": target.database},
            ).scalar()
            if not exists:
                conn.execute(sa.text(f'CREATE DATABASE "{target.database}"'))
    finally:
        admin.dispose()


def create_fresh_test_db(url: str | sa.URL) -> sa.URL:
    """Drop and recreate the target database; returns the target ``sa.URL``.

    Intended for test suites that need a completely clean database for each
    session (e.g. pgvector tests that register custom tables).
    """
    target = sa.engine.make_url(url)
    admin = _maintenance_engine(target)
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{target.database}"'))
            owner = target.username or "postgres"
            conn.execute(
                sa.text(f'CREATE DATABASE "{target.database}" OWNER "{owner}"')
            )
    finally:
        admin.dispose()
    return target


def drop_test_db(url: str | sa.URL) -> None:
    """Terminate open connections and drop the target database."""
    target = sa.engine.make_url(url)
    admin = _maintenance_engine(target)
    try:
        with admin.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": target.database},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{target.database}"'))
    finally:
        admin.dispose()


def ensure_test_user_exists(test_url: str | sa.URL) -> None:
    """Create the test database role if it does not already exist.

    Finds admin credentials by locating a non-test-only database on the same
    host in the stack config. No-op if no matching admin database is found.

    This is PostgreSQL-specific — see plan future work for dialect-agnostic
    user provisioning.
    """
    target = sa.engine.make_url(test_url)
    if not target.username:
        return

    from .loader import load_stack_config
    config = load_stack_config()

    admin_db = next(
        (db for db in config.databases.values()
         if not db.test_only and db.host == target.host),
        None,
    )
    if admin_db is None:
        return

    admin_url = sa.engine.make_url(admin_db.build_url()).set(database="postgres")
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_roles WHERE rolname = :n"),
                {"n": target.username},
            ).scalar()
            # DDL doesn't support bound parameters — escape the values directly.
            username = target.username.replace('"', '""')
            password = (target.password or "").replace("'", "''")
            if not exists:
                conn.execute(sa.text(
                    f'CREATE USER "{username}" WITH SUPERUSER CREATEDB REPLICATION PASSWORD \'{password}\''
                ))
            else:
                attrs = conn.execute(
                    sa.text("SELECT usesuper, usecreatedb, userepl FROM pg_user WHERE usename = :n"),
                    {"n": target.username},
                ).one_or_none()
                if attrs and not (attrs.usesuper and attrs.usecreatedb and attrs.userepl):
                    conn.execute(sa.text(f'ALTER USER "{username}" WITH SUPERUSER CREATEDB REPLICATION'))
    finally:
        admin.dispose()


def ensure_db_extension_exists(db_url: str | sa.URL, extension: str) -> None:
    """Ensure a PostgreSQL extension is installed in the target database.

    Uses admin credentials (same logic as ``ensure_test_user_exists``) because
    ``CREATE EXTENSION`` typically requires superuser.  No-op if no admin DB is
    found on the same host.
    """
    target = sa.engine.make_url(db_url)

    from .loader import load_stack_config
    config = load_stack_config()

    admin_db = next(
        (db for db in config.databases.values()
         if not db.test_only and db.host == target.host),
        None,
    )
    if admin_db is None:
        return

    admin_url = sa.engine.make_url(admin_db.build_url()).set(database=target.database)
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(f"CREATE EXTENSION IF NOT EXISTS {extension} CASCADE"))
    finally:
        admin.dispose()


try:
    import pytest
except ImportError:
    # Not running under pytest — nothing to register, module stays importable.
    pass
else:
    from .package_base import ResourceSpec
    from .resolver import Resolver

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _resource_names(arg: object) -> list[str]:
        if isinstance(arg, ResourceSpec):
            return [arg.semantic_name]
        return [str(arg)]

    def _skip_message(name: str) -> str:
        return (
            f"Resource {name!r} not configured.\n"
            f"  Run: omop-config configure <package>\n"
            f"  (answer Y when asked to configure a test database resource)"
        )

    # ---------------------------------------------------------------------------
    # Plugin hooks
    # ---------------------------------------------------------------------------

    def pytest_configure(config: pytest.Config) -> None:
        config.addinivalue_line(
            "markers",
            "requires_resource(*args): skip when a named OA_Configurator resource is absent. "
            "Accepts a ResourceSpec (e.g. OrmLoaderConfig.TEST_DB) or a resource-name string.",
        )

    def pytest_runtest_setup(item: pytest.Item) -> None:
        for marker in item.iter_markers("requires_resource"):
            for arg in marker.args:
                for name in _resource_names(arg):
                    try:
                        Resolver.from_active_config().resolve_resource(name)
                    except Exception:
                        pytest.skip(_skip_message(name))

    # ---------------------------------------------------------------------------
    # Fixture-level helper (used in conftest.py)
    # ---------------------------------------------------------------------------

    def resolve_test_resource(spec_or_name: ResourceSpec | str) -> str:
        """Return the database URL for a test resource, or ``pytest.skip()`` the test.

        Designed for use inside session-scoped fixtures in conftest.py::

            @pytest.fixture(scope="session")
            def pg_engine():
                url = resolve_test_resource(OmopAlchemyConfig.TEST_DB)
                engine = sa.create_engine(url)
                yield engine
                engine.dispose()

        Parameters
        ----------
        spec_or_name:
            A ``ResourceSpec`` (preferred — no string duplication) or a bare resource
            name string.
        """
        name = (
            spec_or_name.semantic_name
            if isinstance(spec_or_name, ResourceSpec)
            else str(spec_or_name)
        )
        try:
            return Resolver.from_active_config().resolve_resource(name).database.url
        except Exception:
            pytest.skip(_skip_message(name))
