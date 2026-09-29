"""SQLite test-database provisioning strategy.

Isolation is already free here. Every call gets a brand-new, disposable
file database, so there's nothing shared to protect and no savepoint
wrapping is needed (unlike Postgres, whose ``isolated_database()`` has to isolate
against a persistent, shared server). Same public interface either way
(:class:`~oa_configurator.testing.base.IsolatedTestDatabase`); the internal
mechanism achieving isolation is allowed to differ per dialect, and should,
since each one gets whatever's actually cheapest and most natural for it.
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Iterable

import sqlalchemy as sa
import sqlalchemy.orm as so

from .base import IsolatedTestDatabase, TestDatabaseStrategy

if TYPE_CHECKING:
    from ..domains.resources.schema import (
        ResolvedConnection, 
        ResolvedDatabase,
        SchemaClaim,
    )


class SQLiteTestStrategy(TestDatabaseStrategy):
    """Test-database provisioning for SQLite.

    Internal implementation detail of ``isolated_test_database()`` --
    not part of the public surface. SQLite needs no server or credentials,
    so it's the one strategy that overrides ``resolve_without_config()``.
    """

    def resolve_without_config(self) -> "ResolvedDatabase":
        """Fabricate a ``ResolvedDatabase`` pointing at an in-memory target.

        The URL is only used by ``isolated_test_database()`` to determine
        the dialect name -- ``isolated_database()`` below ignores *resolved*
        entirely and always provisions its own fresh tempfile database.
        """
        from ..domains.resources.schema import ResolvedConnection, ResolvedDatabase

        url = "sqlite:///:memory:"
        connection = ResolvedConnection(
            name="sqlite-in-memory",
            url=url,
            safe_url=url,
            _engine_url=sa.engine.make_url(url),
        )
        return ResolvedDatabase(name="sqlite-in-memory", connection=connection, schema_name=None)

    @contextmanager
    def isolated_database(
        self,
        resolved: "ResolvedDatabase | None" = None,
        *,
        extensions: Sequence[str] = (),
        schema_claims: Iterable["SchemaClaim"] = (),
        execution_options: dict[str, Any] | None = None
    ) -> Iterator[IsolatedTestDatabase]:
        """Yield an isolated SQLite database in a fresh tempfile.

        Parameters
        ----------
        resolved : ResolvedDatabase, optional
            Not used to build the engine below, as SQLite has nothing to
            resolve against. Exposed unchanged on the yielded
            IsolatedTestDatabase for callers that want it. Defaulted to
            None only to match TestDatabaseStrategy's shared signature for
            a caller invoking the strategy directly.
        extensions : Sequence[str], optional
            A Postgres-only concept (pgvector etc.). Accepted and silently
            ignored here so callers don't need dialect-specific branching
            just to call isolated_test_database() uniformly.
        schema_claims : Iterable[SchemaClaim], optional
            A list of schema claims to be registered with the isolated database.
        execution_options : dict[str, Any] | None, optional
            Options to be passed to the database engine.
        **engine_kwargs
            Forwarded to ``ResolvedDatabase.create_engine()``.
        """
        from ..domains.resources.schema import ResolvedConnection, ResolvedCDMDatabase

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            url = f"sqlite:///{db_path}"
            connection = ResolvedConnection(name="sqlite-isolated", url=url, safe_url=url, _engine_url=sa.engine.make_url(url))
            fresh = ResolvedCDMDatabase(
                name="sqlite-isolated",
                connection=connection,
                schema_name=None,
                vocab_connection=connection,
                vocab_schema=None,
                results_schema=None,
            )
            engine = fresh.create_engine(schema_claims=schema_claims, execution_options=execution_options)
            try:
                connection = engine.connect()
                try:
                    session = so.Session(bind=connection)
                    try:
                        yield IsolatedTestDatabase(connection=connection, session=session, resolved=resolved)
                    finally:
                        session.close()
                finally:
                    connection.close()
            finally:
                engine.dispose()

    def temporary_schema(self, engine: sa.Engine, *, prefix: str = "test"):
        """Always raises. SQLite cannot satisfy this method's contract.

        SQLite's closest equivalent is ATTACH DATABASE. It only affects
        the one connection that runs it. A second connection, even to
        the same file, never sees it. This was tested directly, not
        assumed. Implementing this anyway would look like it works, then
        fail the moment a genuinely separate connection is involved,
        which is the only real reason this method exists at all.

        Parameters
        ----------
        engine : sqlalchemy.engine.Engine
            Unused. Kept to match the abstract method's signature.
        prefix : str, optional
            Unused. Kept to match the abstract method's signature.

        Raises
        ------
        NotImplementedError
            Always. Use ``isolated_test_database()`` instead, which is
            already free for SQLite.
        """
        raise NotImplementedError(
            "SQLite cannot implement temporary_schema(). ATTACH DATABASE "
            "only works for the connection that runs it. A second "
            "connection never sees it. Use isolated_test_database() "
            "instead."
        )

    def drop_test_database(self, connection: "ResolvedConnection") -> bool:
        """Always raises. Nothing to drop.

        SQLite test databases are disposable tempfiles that clean
        themselves up when isolated_test_database() exits.
        """
        raise NotImplementedError(
            "SQLite has no leftover test databases to drop; isolated_test_database() "
            "already cleans up its own tempfile on exit."
        )
