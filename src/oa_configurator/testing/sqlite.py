"""SQLite test-database provisioning strategy.

Isolation is already free here -- every call gets a brand-new, disposable
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
from typing import Iterator

import sqlalchemy as sa
import sqlalchemy.orm as so

from .base import IsolatedTestDatabase, TestDatabaseStrategy


class SQLiteTestStrategy(TestDatabaseStrategy):
    """Test-database provisioning for SQLite."""

    @contextmanager
    def isolated_database(
        self,
        resolved,
        *,
        extensions: Sequence[str] = (),
        **engine_kwargs: object,
    ) -> Iterator[IsolatedTestDatabase]:
        # extensions is a Postgres-only concept (pgvector etc.) -- accepted
        # and silently ignored here so callers don't need dialect-specific
        # branching just to call isolated_test_database() uniformly.
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            engine = sa.create_engine(f"sqlite:///{db_path}", **engine_kwargs)
            try:
                connection = engine.connect()
                try:
                    session = so.Session(bind=connection)
                    try:
                        yield IsolatedTestDatabase(connection=connection, session=session)
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
