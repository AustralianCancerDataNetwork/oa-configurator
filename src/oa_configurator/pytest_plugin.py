"""OA_Configurator pytest plugin, auto-loaded via the ``pytest11`` entry point.

# NOTE: This is currently pgvector heavy. A future feature request will support other backends.

Provides the ``requires_database`` marker, the ``resolve_test_database(cls,
field_name)`` fixture helper (resolves a package's own named test-database
field, see its docstring for why it doesn't just go through
``cls.get_config()``), and standalone PostgreSQL database lifecycle utilities:

- ``ensure_test_db_exists(url)``: creates the target database if absent
- ``create_fresh_test_db(url)``: drops and recreates (used by omop-emb)
- ``drop_test_db(url)``: terminates connections and drops the database
- ``require_pg_extension(url, ext)``: skips if a required extension is absent

All DDL-construction uses ``psycopg.sql.Identifier`` / ``Literal`` for safe
quoting. Admin credentials are sourced from the stack config (non-test-only DB
on the same host); the admin account owns/creates the databases. Test users are
created with ``SUPERUSER LOGIN`` (SUPERUSER is required by orm-loader's FK
bypass via ``session_replication_role``; CREATEDB and REPLICATION are not
granted). A fallback to the test user's own credentials is used when no admin
DB is found (standalone containers where the test user is the superuser).

The database argument to ``requires_database`` is a plain database-name
string, e.g. ``@pytest.mark.requires_database("test_cdm_db")``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa


def _pg_ident(name: str) -> object:
    from psycopg.sql import Identifier
    return Identifier(name)


def _pg_lit(value: str) -> object:
    from psycopg.sql import Literal
    return Literal(value)


def _pg_ddl(template: str, *parts: object) -> str:
    """Build a safe DDL string using psycopg.sql quoting (lazy import)."""
    from psycopg.sql import SQL
    return SQL(template).format(*parts).as_string(None)  # ty: ignore[invalid-argument-type]


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
    admin_conn = next(
        (conn for conn in config.connections.values()
         if not conn.test_only and conn.host == host),
        None,
    )
    if admin_conn is None:
        return None
    return sa.engine.make_url(admin_conn.build_url())


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
    Safe to call repeatedly, since it is idempotent.
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
    REPLICATION are not granted, since the admin account is the one that
    creates databases.

    This is PostgreSQL-specific. See the feature request for dialect-agnostic
    user provisioning, and the FK-bypass-without-SUPERUSER feature request,
    for the long-term fix.
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
    superuser). This function only checks whether the extension is present.
    It never creates one itself.

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
    # Not running under pytest, so there is nothing to register. The module
    # stays importable regardless.
    pass
else:
    from .loader import load_stack_config
    from .package_base import PackageConfigBase
    from .resolver import Resolver

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _skip_message(name: str) -> str:
        return (
            f"Database {name!r} not configured.\n"
            f"  Run: omop-config databases add {name} ...\n"
            f"  (or configure it interactively via omop-config configure <package>)"
        )

    def _not_test_only_message(name: str, connection_name: str) -> str:
        return (
            f"SAFETY ABORT: database {name!r} resolves to connection {connection_name!r}, "
            "which is not marked test_only=true.\n"
            "  Refusing to use it as a test database -- this guards against tests running"
            " destructive operations (DROP SCHEMA, TRUNCATE, ...) against real data.\n"
            f"  Run: omop-config connections add {connection_name} ... --test-only true"
            " (or mark the existing connection test_only=true directly in config.toml)"
        )

    # ---------------------------------------------------------------------------
    # Plugin hooks
    # ---------------------------------------------------------------------------

    def pytest_configure(config: pytest.Config) -> None:
        config.addinivalue_line(
            "markers",
            "requires_database(*args): skip when a named OA_Configurator database is absent. "
            "Accepts one or more database-name strings.",
        )

    def pytest_runtest_setup(item: pytest.Item) -> None:
        for marker in item.iter_markers("requires_database"):
            for name in marker.args:
                try:
                    Resolver.from_active_config().resolve_database(str(name))
                except Exception:
                    pytest.skip(_skip_message(str(name)))

    # ---------------------------------------------------------------------------
    # Fixture-level helper (used in conftest.py)
    # ---------------------------------------------------------------------------

    def resolve_test_database(cls: type[PackageConfigBase], field_name: str) -> str:
        """Resolve a package's own test-database field to a URL, or skip/fail.

        *field_name* names the field directly, e.g. ``"test_cdm_db"`` leads to
        no auto-discovery. Use-case: Multiple ``RefTo(DatabaseConfig, is_test=True)`` 
        fields, that are otherwise not auto-discoverable.

        Deliberately does not go through ``cls.get_config()`` to avoid
        validating every ``RefTo`` field on the class at once. A CI runner 
        usually does not have production resources configured, which would
        fail the test before it even reaches the test database field.
        This resolves just the one named field directly off ``[tools.<name>]``, 
        tolerating everything else on the class being unconfigured.

        Load-bearing safety check: the resolved database's underlying
        connection must be marked ``test_only=true`` in the stack config, or
        this fails loudly (``pytest.fail``).

        Parameters
        ----------
        cls : type[PackageConfigBase]
            The package's config class.
        field_name : str
            Name of the ``is_test`` field/attribute to resolve from the respective `PackageConfigBase`, 
            e.g. ``"test_cdm_db"``.

        Returns
        -------
        str
            Connection URL, or the test is skipped/failed (see above).

        Designed for use inside session-scoped fixtures in conftest.py::

            @pytest.fixture(scope="session")
            def pg_engine():
                url = resolve_test_database(OmopAlchemyConfig, "test_cdm_db")
                engine = sa.create_engine(url)
                yield engine
                engine.dispose()
        """
        try:
            stored = load_stack_config().tools.get(cls.tool_name, {})
        except FileNotFoundError:
            stored = {}
        name = stored.get(field_name, field_name)

        try:
            resolver = Resolver.from_active_config()
            resolved = resolver.resolve_database(name)
        except Exception:
            pytest.skip(_skip_message(name))
        connection_name = resolved.connection.name
        if not resolver.config.connections[connection_name].test_only:
            pytest.fail(_not_test_only_message(name, connection_name))
        return resolved.connection.url
