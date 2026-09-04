"""PostgreSQL test-database provisioning strategy."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

import pytest
import sqlalchemy as sa
import sqlalchemy.orm as so
from sqlalchemy import exc as sa_exc

from .base import IsolatedTestDatabase, TestDatabaseStrategy

if TYPE_CHECKING:
    from ..domains.resources.schema import ResolvedConnection


def _pg_ident(name: str) -> object:
    from psycopg.sql import Identifier

    return Identifier(name)


def _pg_ddl(template: str, *parts: object) -> str:
    """Build a safe DDL string using psycopg.sql quoting (lazy import)."""
    from psycopg.sql import SQL

    return SQL(template).format(*parts).as_string(None)  # ty: ignore[invalid-argument-type]


def _production_target_message(target: sa.URL, match: str) -> str:
    return (
        f"SAFETY ABORT: test database target ({target.host}:{target.port}/{target.database}) "
        f"matches the non-test connection {match!r} (same host, database name, and port).\n"
        "  Refusing to provision it.\n"
        "  Use a different host, database name, or port for the test connection."
    )


def _create_database_permission_message(db_name: str, username: str | None) -> str:
    return (
        f"Could not create database {db_name!r} as {username!r}: insufficient privileges.\n"
        f"  Grant CREATEDB to {username!r}, or create {db_name!r} yourself before running tests."
    )


class PostgresTestStrategy(TestDatabaseStrategy):
    """Test-database provisioning for PostgreSQL.

    Uses the configured connection's own credentials for every operation,
    including creating the target database itself.  A connection that can't 
    create its own database fails with a clear, actionable error.
    """

    def _guard_against_production_target(self, target: sa.URL) -> None:
        """Abort if target's host/database/port matches a non-test_only connection.

        A test_only connection's own details are already checked against
        this at ``connections add`` time (``_check_test_collision``). This
        is the same check (``_find_production_collision``), reapplied here
        at provisioning time, catching a ``config.toml`` hand-edited to
        bypass that check before CREATE DATABASE ever runs against it.
        """
        from ..loader import load_stack_config
        from ..resolver import _find_production_collision

        try:
            config = load_stack_config()
        except (FileNotFoundError, ValueError):
            return
        match = _find_production_collision(target.host, target.database, target.port, config)
        if match is not None:
            pytest.fail(_production_target_message(target, match))

    def _ensure_test_db_exists(self, url: str | sa.URL) -> None:
        """Create the target database if it does not already exist.

        Connects as the target connection's own user. 
        Race-safe under concurrent runs (e.g. ``pytest -n``):
        Postgres has no ``CREATE DATABASE IF NOT EXISTS``, so this
        attempts the create directly and catches the "already exists"
        error, rather than checking existence first and creating second,
        which leaves a real window for two runs to race.
        """
        from psycopg.errors import DuplicateDatabase, InsufficientPrivilege

        target = sa.engine.make_url(url)
        db_name = target.database
        if db_name is None:
            return
        self._guard_against_production_target(target)
        # CREATE DATABASE needs the maintenance DB, not the not-yet-created target.
        engine = sa.create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as conn:
                try:
                    conn.execute(sa.text(_pg_ddl("CREATE DATABASE {}", _pg_ident(db_name))))
                except sa_exc.ProgrammingError as exc:
                    if isinstance(exc.orig, DuplicateDatabase):
                        pass
                    elif isinstance(exc.orig, InsufficientPrivilege):
                        pytest.fail(_create_database_permission_message(db_name, target.username))
                    else:
                        raise
        finally:
            engine.dispose()

    def _install_extensions(self, connection: "ResolvedConnection", extensions: Sequence[str]) -> None:
        if not extensions:
            return
        ext_engine = connection.create_engine(isolation_level="AUTOCOMMIT")
        try:
            with ext_engine.connect() as conn:
                for ext in extensions:
                    conn.execute(sa.text(_pg_ddl("CREATE EXTENSION IF NOT EXISTS {}", _pg_ident(ext))))
        finally:
            ext_engine.dispose()

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
        self._ensure_test_db_exists(url)
        if extensions:
            self._install_extensions(resolved.connection, extensions)

        engine = resolved.create_engine(**engine_kwargs)
        try:
            connection = engine.connect()
            trans = connection.begin()
            try:
                session = so.Session(bind=connection, join_transaction_mode="create_savepoint")
                try:
                    yield IsolatedTestDatabase(connection=connection, session=session, resolved=resolved)
                finally:
                    session.close()
            finally:
                trans.rollback()
                connection.close()
        finally:
            engine.dispose()

    @contextmanager
    def temporary_schema(self, engine: sa.Engine, *, prefix: str = "test") -> Iterator[str]:
        self._require_test_only_engine(engine)
        schema = f"{prefix}_{uuid.uuid4().hex[:12]}"
        with engine.begin() as conn:
            conn.execute(sa.text(_pg_ddl("CREATE SCHEMA {}", _pg_ident(schema))))
        try:
            yield schema
        finally:
            with engine.begin() as conn:
                conn.execute(sa.text(_pg_ddl("DROP SCHEMA IF EXISTS {} CASCADE", _pg_ident(schema))))
