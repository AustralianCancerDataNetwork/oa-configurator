"""DuckDB test-database provisioning strategy.

Like SQLite, isolation is free for ``isolated_database()``: every call gets
a brand-new, disposable file database. Unlike SQLite, DuckDB genuinely
supports a committed schema visible from a second connection on the same
engine, so ``temporary_schema()`` follows Postgres's pattern instead of
SQLite's hard-raising one.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import sqlalchemy.orm as so

from ..domains.resources.sql import Role
from .base import IsolatedTestDatabase, TestDatabaseStrategy

if TYPE_CHECKING:
    from ..domains.resources.schema import (
        ResolvedCDMDatabase,
        ResolvedConnection,
        ResolvedDatabase,
        SchemaClaim,
    )


class DuckDBTestStrategy(TestDatabaseStrategy):
    """Test-database provisioning for DuckDB.

    Internal implementation detail of ``isolated_test_database()``, not
    part of the public surface. DuckDB needs no server or credentials, so
    it's the second strategy (after SQLite) that overrides
    ``resolve_without_config()``.
    """

    def resolve_without_config(self) -> ResolvedCDMDatabase:
        """Fabricate a ``ResolvedCDMDatabase`` pointing at an in-memory target.

        The URL is only used by ``isolated_test_database()`` to determine
        the dialect name. ``isolated_database()`` below ignores *resolved*
        entirely and always provisions its own fresh tempfile database.
        """
        from ..domains.resources.schema import ResolvedCDMDatabase, ResolvedConnection

        url = "duckdb:///:memory:"
        connection = ResolvedConnection(
            name="duckdb-in-memory",
            url=url,
            safe_url=url,
            _engine_url=sa.engine.make_url(url),
        )
        return ResolvedCDMDatabase(
            name="duckdb-in-memory",
            connection=connection,
            schema_name=None,
            vocab_connection=connection,
            vocab_schema=None,
            results_schema=None,
        )

    @contextmanager
    def isolated_database(
        self,
        resolved: ResolvedDatabase | None = None,
        *,
        schema_claims: Iterable[SchemaClaim] = (),
        execution_options: dict[str, Any] | None = None,
        **engine_kwargs: Any,
    ) -> Generator[IsolatedTestDatabase, None, None]:
        """Yield an isolated DuckDB database in a fresh tempfile.

        Parameters
        ----------
        resolved : ResolvedDatabase, optional
            Unused: the yielded IsolatedTestDatabase carries the
            ResolvedCDMDatabase built for the tempfile instead. Kept to
            match TestDatabaseStrategy's shared signature.
        schema_claims : Iterable[SchemaClaim], optional
            A list of schema claims to be registered with the isolated database.
        execution_options : dict[str, Any] | None, optional
            Options to be passed to the database engine.
        **engine_kwargs
            Forwarded to the primary engine build, e.g. ``extensions`` for a
            connect-event callable (such as the VSS extension loader) the
            engine needs on every physical connection.
        """
        from ..domains.resources.schema import ResolvedCDMDatabase, ResolvedConnection

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.duckdb"
            url = f"duckdb:///{db_path}"
            connection = ResolvedConnection(
                name="duckdb-isolated", url=url, safe_url=url, _engine_url=sa.engine.make_url(url), test_only=True
            )
            fresh = resolved or replace(self.resolve_without_config(), name="duckdb-isolated")
            if isinstance(fresh, ResolvedCDMDatabase):
                fresh = replace(fresh, connection=connection, vocab_connection=connection)
            else:
                fresh = replace(fresh, connection=connection)
            engine = fresh._build_engine(
                Role.PRIMARY,
                schema_claims=schema_claims, execution_options=execution_options, **engine_kwargs
            )
            try:
                connection = engine.connect()
                try:
                    session = so.Session(bind=connection)
                    try:
                        yield IsolatedTestDatabase(connection=connection, session=session, resolved=fresh)
                    finally:
                        session.close()
                finally:
                    connection.close()
            finally:
                engine.dispose()

    @contextmanager
    def temporary_schema(self, engine: sa.Engine, *, prefix: str = "test") -> Generator[str, None, None]:
        """Yield a uniquely-named, genuinely-committed schema, dropped on exit.

        A second connection opened from the same engine sees
        a schema (and its tables) committed by another connection, so this
        follows Postgres's real-schema pattern rather than SQLite's
        hard-raising one.
        """
        self._require_test_only_engine(engine)
        schema = f"{prefix}_{uuid.uuid4().hex[:12]}"
        with engine.begin() as conn:
            conn.execute(sa.schema.CreateSchema(schema))
        try:
            yield schema
        finally:
            with engine.begin() as conn:
                conn.execute(sa.schema.DropSchema(schema, cascade=True))

    def drop_test_database(self, connection: ResolvedConnection) -> bool:
        """Always raises. Nothing to drop.

        DuckDB test databases are disposable tempfiles that clean
        themselves up when isolated_test_database() exits.
        """
        raise NotImplementedError(
            "DuckDB has no leftover test databases to drop; isolated_test_database() "
            "already cleans up its own tempfile on exit."
        )
