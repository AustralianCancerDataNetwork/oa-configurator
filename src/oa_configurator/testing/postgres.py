"""PostgreSQL test-database provisioning strategy.

Every non-destructive admin/DDL operation the old flat ``pytest_plugin.py``
exposed as standalone functions lives here now, as private methods -- this
class is the one place PostgreSQL test-database lifecycle logic lives, not
a class that calls out to functions scattered elsewhere.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Iterator

import sqlalchemy as sa
import sqlalchemy.orm as so

from .base import IsolatedTestDatabase, TestDatabaseStrategy


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


class PostgresTestStrategy(TestDatabaseStrategy):
    """Test-database provisioning for PostgreSQL."""

    # -- admin credentials ---------------------------------------------------

    def _find_admin_url(self, host: str | None) -> sa.URL | None:
        """Return admin (non-test-only) DB credentials on the same host, or None."""
        if host is None:
            return None
        from ..loader import load_stack_config

        try:
            config = load_stack_config()
        except (FileNotFoundError, ValueError):
            return None
        admin_conn = next(
            (
                conn
                for conn in config.connections.values()
                if not conn.test_only and conn.host == host
            ),
            None,
        )
        if admin_conn is None:
            return None
        return sa.engine.make_url(admin_conn.build_url())

    def _admin_engine(self, test_url: sa.URL) -> sa.Engine:
        """Engine on the 'postgres' maintenance DB.

        Prefers admin credentials from the stack config; falls back to the
        test user's own credentials when running in a standalone container
        where the test user is the superuser.
        """
        base = self._find_admin_url(test_url.host) or test_url
        return sa.create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")

    def _refuse_if_production(self, target: sa.URL) -> None:
        """Abort if target's host/database name/port match a non-test_only
        connection in the stack config, regardless of how that connection is
        (or isn't) wired to a database entry."""
        if target.database is None:
            return
        from ..loader import load_stack_config
        from ..resolver import _find_production_collision

        try:
            config = load_stack_config()
        except (FileNotFoundError, ValueError):
            return
        match = _find_production_collision(target.host, target.database, target.port, config)
        if match is not None:
            raise RuntimeError(
                f"SAFETY ABORT: refusing to drop/recreate database {target.database!r} on "
                f"{target.host!r}: it matches the non-test connection {match!r} in the stack "
                f"config. This would destroy production data."
            )

    # -- non-destructive provisioning (idempotent, safe under concurrency) --

    def _ensure_test_db_exists(self, url: str | sa.URL) -> None:
        """Create the target database if it does not already exist.

        Uses admin credentials (non-test-only DB on same host) to create the
        database and sets the test user as OWNER, granting them full control
        within the database without needing CREATEDB or SUPERUSER.

        Falls back to the test user's own credentials when no admin DB is
        found. Safe to call repeatedly, since it is idempotent.
        """
        target = sa.engine.make_url(url)
        db_name = target.database
        if db_name is None:
            return
        admin = self._admin_engine(target)
        try:
            with admin.connect() as conn:
                exists = conn.execute(
                    sa.text("SELECT 1 FROM pg_database WHERE datname = :n"),
                    {"n": db_name},
                ).scalar()
                if not exists:
                    owner = target.username or "postgres"
                    conn.execute(
                        sa.text(
                            _pg_ddl(
                                "CREATE DATABASE {} OWNER {}",
                                _pg_ident(db_name),
                                _pg_ident(owner),
                            )
                        )
                    )
        finally:
            admin.dispose()

    def _ensure_test_user_exists(self, test_url: str | sa.URL) -> None:
        """Create the test database role if it does not already exist.

        SUPERUSER is granted because orm-loader's bulk FK-bypass path uses
        ``SET session_replication_role = 'replica'``, which requires
        SUPERUSER in PostgreSQL (no narrower privilege exists;
        ``ALTER TABLE ... DISABLE TRIGGER ALL`` has the same requirement for
        FK constraint triggers). CREATEDB and REPLICATION are not granted,
        since the admin account is the one that creates databases.
        """
        target = sa.engine.make_url(test_url)
        username = target.username
        if not username:
            return

        admin_url = self._find_admin_url(target.host)
        if admin_url is None:
            return

        admin = sa.create_engine(admin_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                exists = conn.execute(
                    sa.text("SELECT 1 FROM pg_roles WHERE rolname = :n"),
                    {"n": username},
                ).scalar()
                if not exists:
                    conn.execute(
                        sa.text(
                            _pg_ddl(
                                "CREATE USER {} WITH SUPERUSER LOGIN PASSWORD {}",
                                _pg_ident(username),
                                _pg_lit(target.password or ""),
                            )
                        )
                    )
                else:
                    attrs = conn.execute(
                        sa.text("SELECT rolsuper, rolcanlogin FROM pg_roles WHERE rolname = :n"),
                        {"n": username},
                    ).one_or_none()
                    if attrs and not (attrs.rolsuper and attrs.rolcanlogin):
                        conn.execute(
                            sa.text(_pg_ddl("ALTER USER {} WITH SUPERUSER LOGIN", _pg_ident(username)))
                        )
        finally:
            admin.dispose()

    def _install_extensions(self, url: sa.URL, extensions: Sequence[str]) -> None:
        if not extensions:
            return
        admin_base = self._find_admin_url(url.host) or url
        ext_engine = sa.create_engine(
            admin_base.set(database=url.database), isolation_level="AUTOCOMMIT"
        )
        try:
            with ext_engine.connect() as conn:
                for ext in extensions:
                    conn.execute(sa.text(_pg_ddl("CREATE EXTENSION IF NOT EXISTS {}", _pg_ident(ext))))
        finally:
            ext_engine.dispose()

    # -- destructive provisioning (kept for the deprecated public wrappers;
    #    isolated_database() itself, below, is non-destructive) --------------

    def _create_fresh_test_db(self, url: str | sa.URL, *, extensions: Sequence[str] = ()) -> sa.URL:
        """Drop and recreate the target database; returns the target sa.URL."""
        target = sa.engine.make_url(url)
        db_name = target.database
        if db_name is None:
            return target
        self._refuse_if_production(target)
        admin = self._admin_engine(target)
        try:
            with admin.connect() as conn:
                conn.execute(sa.text(_pg_ddl("DROP DATABASE IF EXISTS {}", _pg_ident(db_name))))
                owner = target.username or "postgres"
                conn.execute(
                    sa.text(
                        _pg_ddl(
                            "CREATE DATABASE {} OWNER {}",
                            _pg_ident(db_name),
                            _pg_ident(owner),
                        )
                    )
                )
        finally:
            admin.dispose()
        self._install_extensions(target, extensions)
        return target

    def _drop_test_db(self, url: str | sa.URL) -> None:
        """Terminate open connections and drop the target database."""
        target = sa.engine.make_url(url)
        db_name = target.database
        if db_name is None:
            return
        self._refuse_if_production(target)
        admin = self._admin_engine(target)
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

    def _require_pg_extension(self, db_url: str | sa.URL, extension: str) -> None:
        """Skip the test session if a required PostgreSQL extension is not installed."""
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
            import pytest

            pytest.skip(
                f"PostgreSQL extension {extension!r} is not installed in "
                f"{target.database!r}. Pre-install it via a container init script "
                f"or run: psql -U postgres -c 'CREATE EXTENSION {extension}'"
            )

    # -- TestDatabaseStrategy interface --------------------------------------

    @contextmanager
    def isolated_database(
        self,
        resolved,
        *,
        extensions: Sequence[str] = (),
        **engine_kwargs: object,
    ) -> Iterator[IsolatedTestDatabase]:
        url = resolved.connection.url
        self._ensure_test_user_exists(url)
        self._ensure_test_db_exists(url)
        if extensions:
            self._install_extensions(sa.engine.make_url(url), extensions)

        engine = resolved.create_engine(**engine_kwargs)
        try:
            connection = engine.connect()
            trans = connection.begin()
            try:
                session = so.Session(bind=connection, join_transaction_mode="create_savepoint")
                try:
                    yield IsolatedTestDatabase(connection=connection, session=session)
                finally:
                    session.close()
            finally:
                trans.rollback()
                connection.close()
        finally:
            engine.dispose()

    @contextmanager
    def temporary_schema(self, engine: sa.Engine, *, prefix: str = "test") -> Iterator[str]:
        schema = f"{prefix}_{uuid.uuid4().hex[:12]}"
        with engine.begin() as conn:
            conn.execute(sa.text(_pg_ddl("CREATE SCHEMA {}", _pg_ident(schema))))
        try:
            yield schema
        finally:
            with engine.begin() as conn:
                conn.execute(sa.text(_pg_ddl("DROP SCHEMA IF EXISTS {} CASCADE", _pg_ident(schema))))
