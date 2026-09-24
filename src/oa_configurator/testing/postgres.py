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

from ..domains.resources.rectify import _refuse_production_collision
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


class PostgresTestStrategy(TestDatabaseStrategy):
    """Test-database provisioning for PostgreSQL.

    Uses the configured connection's own credentials for every operation,
    including creating the target database itself.  A connection that can't
    create its own database fails with a clear, actionable error.
    """

    def _ensure_test_db_exists(self, url: str | sa.URL) -> None:
        """Create the target database if it does not already exist.

        Race-safe under concurrent runs (e.g. ``pytest -n``): Postgres has
        no ``CREATE DATABASE IF NOT EXISTS``, so this attempts the create
        directly and catches the "already exists" error.
        """
        from psycopg.errors import DuplicateDatabase, InsufficientPrivilege

        target = sa.engine.make_url(url)
        db_name = target.database
        if db_name is None:
            return
        try:
            _refuse_production_collision(target)
        except RuntimeError as exc:
            pytest.fail(str(exc))
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
                        pytest.fail(
                            f"Could not create database {db_name!r} as {target.username!r}: "
                            "insufficient privileges."
                        )
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

    def drop_test_database(self, connection: "ResolvedConnection") -> bool:
        """Drop connection's database if it exists.

        Raises
        ------
        ValueError
            If connection isn't test_only, or names a system database.
        """
        if not connection.test_only:
            raise ValueError(f"Refusing to drop database for non-test connection {connection.name!r}.")
        target = sa.engine.make_url(connection.url)
        if target.database in (None, "postgres", "template0", "template1"):
            raise ValueError(f"Refusing to drop database {target.database!r}.")
        _refuse_production_collision(target)

        engine = sa.create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as conn:
                try:
                    conn.execute(sa.text(_pg_ddl("DROP DATABASE {}", _pg_ident(target.database))))
                except sa_exc.ProgrammingError as exc:
                    if getattr(exc.orig, "sqlstate", None) == "3D000":
                        return False
                    raise
        finally:
            engine.dispose()
        return True
