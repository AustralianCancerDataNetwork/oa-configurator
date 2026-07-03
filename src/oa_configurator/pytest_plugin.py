"""OA_Configurator pytest plugin — auto-loaded via the ``pytest11`` entry point.

# NOTE: This is currently pgvector heavy. A future feature request will support other backends.

Provides the ``requires_resource`` marker, the ``resolve_test_resource`` fixture
helper, and standalone PostgreSQL database lifecycle utilities:

- ``ensure_test_db_exists(url)`` — create the target database if absent
- ``create_fresh_test_db(url)`` — drop and recreate (used by omop-emb)
- ``drop_test_db(url)`` — terminate connections and drop the database
- ``require_pg_extension(url, ext)`` — skip if a required extension is absent

All DDL-construction uses ``psycopg.sql.Identifier`` / ``Literal`` for safe
quoting. Admin credentials are sourced from the stack config (non-test-only DB
on the same host); the admin account owns/creates the databases. Test users are
created with ``SUPERUSER LOGIN`` (SUPERUSER is required by orm-loader's FK
bypass via ``session_replication_role``; CREATEDB and REPLICATION are not
granted). A fallback to the test user's own credentials is used when no admin
DB is found (standalone containers where the test user is the superuser).

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

from collections.abc import Sequence

import sqlalchemy as sa


def _pg_ident(name: str) -> object:
    from psycopg.sql import Identifier
    return Identifier(name)


def _pg_lit(value: str) -> object:
    from psycopg.sql import Literal
    return Literal(value)  # type: ignore[arg-type]


def _pg_ddl(template: str, *parts: object) -> str:
    """Build a safe DDL string using psycopg.sql quoting (lazy import)."""
    from psycopg.sql import SQL
    return SQL(template).format(*parts).as_string(None)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_admin_url(host: str | None) -> sa.URL | None:
    """Return admin (non-test-only) DB credentials on the same host, or None."""
    if host is None:
        return None
    from .loader import load_stack_config
    try:
        config = load_stack_config()
    except (FileNotFoundError, ValueError):
        return None
    admin_db = next(
        (db for db in config.databases.values()
         if not db.test_only and db.host == host),
        None,
    )
    if admin_db is None:
        return None
    return sa.engine.make_url(admin_db.build_url())


def _admin_engine(test_url: sa.URL) -> sa.Engine:
    """Engine on the 'postgres' maintenance DB.

    Prefers admin credentials from the stack config; falls back to the test
    user's own credentials when running in a standalone container where the
    test user is the superuser.
    """
    base = _find_admin_url(test_url.host) or test_url
    return sa.create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")


# ---------------------------------------------------------------------------
# Public lifecycle helpers
# ---------------------------------------------------------------------------

def ensure_test_db_exists(url: str | sa.URL) -> None:
    """Create the target database if it does not already exist.

    Uses admin credentials (non-test-only DB on same host) to create the
    database and sets the test user as OWNER, granting them full control
    within the database without needing CREATEDB or SUPERUSER.

    Falls back to the test user's own credentials when no admin DB is found.
    Safe to call repeatedly — idempotent.
    """
    target = sa.engine.make_url(url)
    db_name = target.database
    if db_name is None:
        return
    admin = _admin_engine(target)
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": db_name},
            ).scalar()
            if not exists:
                owner = target.username or "postgres"
                conn.execute(sa.text(
                    _pg_ddl("CREATE DATABASE {} OWNER {}", _pg_ident(db_name), _pg_ident(owner))
                ))
    finally:
        admin.dispose()


def create_fresh_test_db(url: str | sa.URL, *, extensions: Sequence[str] = ()) -> sa.URL:
    """Drop and recreate the target database; returns the target ``sa.URL``.

    Intended for test suites that need a completely clean database for each
    session (e.g. pgvector tests that register custom tables).

    Parameters
    ----------
    extensions:
        PostgreSQL extensions to install in the new database immediately after
        creation. Uses admin credentials so the test user does not need
        SUPERUSER. Pass e.g. ``extensions=["vector"]`` to pre-install pgvector
        rather than relying on template1 or an external init script.
    """
    target = sa.engine.make_url(url)
    db_name = target.database
    if db_name is None:
        return target
    admin = _admin_engine(target)
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(_pg_ddl("DROP DATABASE IF EXISTS {}", _pg_ident(db_name))))
            owner = target.username or "postgres"
            conn.execute(sa.text(
                _pg_ddl("CREATE DATABASE {} OWNER {}", _pg_ident(db_name), _pg_ident(owner))
            ))
    finally:
        admin.dispose()
    if extensions:
        admin_base = _find_admin_url(target.host) or target
        ext_engine = sa.create_engine(
            admin_base.set(database=db_name), isolation_level="AUTOCOMMIT"
        )
        try:
            with ext_engine.connect() as conn:
                for ext in extensions:
                    conn.execute(sa.text(
                        _pg_ddl("CREATE EXTENSION IF NOT EXISTS {}", _pg_ident(ext))
                    ))
        finally:
            ext_engine.dispose()
    return target


def drop_test_db(url: str | sa.URL) -> None:
    """Terminate open connections and drop the target database."""
    target = sa.engine.make_url(url)
    db_name = target.database
    if db_name is None:
        return
    admin = _admin_engine(target)
    try:
        with admin.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": db_name},
            )
            conn.execute(sa.text(_pg_ddl("DROP DATABASE IF EXISTS {}", _pg_ident(db_name))))
    finally:
        admin.dispose()


def ensure_test_user_exists(test_url: str | sa.URL) -> None:
    """Create the test database role if it does not already exist.

    Finds admin credentials by locating a non-test-only database on the same
    host in the stack config. No-op if no matching admin database is found.

    SUPERUSER is granted because orm-loader's bulk FK-bypass path uses
    ``SET session_replication_role = 'replica'``, which requires SUPERUSER in
    PostgreSQL (no narrower privilege exists; ``ALTER TABLE ... DISABLE TRIGGER
    ALL`` has the same requirement for FK constraint triggers). CREATEDB and
    REPLICATION are not granted — the admin creates databases.

    This is PostgreSQL-specific — see feature request for dialect-agnostic
    user provisioning and the FK-bypass-without-SUPERUSER feature request for
    the long-term fix.
    """
    target = sa.engine.make_url(test_url)
    username = target.username
    if not username:
        return

    admin_url = _find_admin_url(target.host)
    if admin_url is None:
        return

    admin = sa.create_engine(
        admin_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_roles WHERE rolname = :n"),
                {"n": username},
            ).scalar()
            if not exists:
                conn.execute(sa.text(
                    _pg_ddl(
                        "CREATE USER {} WITH SUPERUSER LOGIN PASSWORD {}",
                        _pg_ident(username), _pg_lit(target.password or ""),
                    )
                ))
            else:
                attrs = conn.execute(
                    sa.text("SELECT rolsuper, rolcanlogin FROM pg_roles WHERE rolname = :n"),
                    {"n": username},
                ).one_or_none()
                if attrs and not (attrs.rolsuper and attrs.rolcanlogin):
                    conn.execute(sa.text(
                        _pg_ddl("ALTER USER {} WITH SUPERUSER LOGIN", _pg_ident(username))
                    ))
    finally:
        admin.dispose()


def require_pg_extension(db_url: str | sa.URL, extension: str) -> None:
    """Skip the test session if a required PostgreSQL extension is not installed.

    Extensions must be pre-installed by a DBA or container init script
    (e.g. an entrypoint that runs ``CREATE EXTENSION`` as the postgres
    superuser). This function only checks — it never creates.

    Must be called from pytest fixture or conftest code (uses ``pytest.skip``).
    """
    target = sa.engine.make_url(db_url)
    engine = sa.create_engine(target)
    try:
        with engine.connect() as conn:
            installed = conn.execute(
                sa.text("SELECT 1 FROM pg_extension WHERE extname = :n"),
                {"n": extension},
            ).scalar()
    finally:
        engine.dispose()
    if not installed:
        import pytest as _pytest
        _pytest.skip(
            f"PostgreSQL extension {extension!r} is not installed in "
            f"{target.database!r}. Pre-install it via a container init script "
            f"or run: psql -U postgres -c 'CREATE EXTENSION {extension}'"
        )


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
            return Resolver.from_active_config().resolve_resource(name).database.build_url()
        except Exception:
            pytest.skip(_skip_message(name))
