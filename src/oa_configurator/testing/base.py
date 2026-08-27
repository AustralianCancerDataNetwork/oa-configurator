"""Per-dialect strategy for isolated test-database provisioning.

Add a new dialect by subclassing :class:`TestDatabaseStrategy` (two
abstract methods) and registering it in ``testing/__init__.py``'s dispatch
table. No other code changes are needed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from ..domains.resources.schema import ResolvedDatabase
    from ..package_base import PackageConfigBase


@dataclass
class IsolatedTestDatabase:
    """An isolated database resource scoped to one test.

    ``.connection`` and ``.session`` share the same underlying transaction.
    Pick whichever fits (Core vs. ORM) for a given test, never mix a
    different one in alongside them.
    """

    connection: Connection
    session: Session


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
        "  Refusing to use it as a test database, since this guards against tests running"
        " destructive operations (DROP SCHEMA, TRUNCATE, ...) against real data.\n"
        f"  Run: omop-config connections add {connection_name} ... --test-only true"
        " (or mark the existing connection test_only=true directly in config.toml)"
    )


def _unknown_engine_message(safe_url: str) -> str:
    return (
        f"SAFETY ABORT: no connection in the active config matches {safe_url!r} "
        "by host/database/port.\n"
        "  Refusing to use it with isolated_test_schema(), since this creates and drops"
        " a real, committed schema and can only be verified test_only=true against a"
        " known connection.\n"
        "  Run: omop-config connections add <name> ... --test-only true"
    )


def _engine_not_test_only_message(connection_name: str) -> str:
    return (
        f"SAFETY ABORT: this engine matches connection {connection_name!r}, which is"
        " not marked test_only=true.\n"
        "  Refusing to use it with isolated_test_schema(), since this creates and drops"
        " a real, committed schema and could destroy real data if pointed at production.\n"
        f"  Run: omop-config connections add {connection_name} ... --test-only true"
        " (or mark the existing connection test_only=true directly in config.toml)"
    )


class TestDatabaseStrategy(ABC):
    """Per-dialect isolation strategy for test-database provisioning."""

    @staticmethod
    def _resolve_and_check(
        config_cls: type["PackageConfigBase"], field_name: str
    ) -> "ResolvedDatabase":
        """Resolve *field_name* off *config_cls* and enforce ``test_only``.

        Dialect-agnostic: resolving a config field and checking a
        connection's ``test_only`` flag doesn't depend on Postgres vs.
        SQLite, so this is one shared step every strategy's ``isolated_database()``
        relies on before diverging into dialect-specific work, not
        duplicated per subclass.
        """
        if field_name not in config_cls.model_fields:
            raise ValueError(f"{config_cls.__name__} has no field {field_name!r}.")

        from ..loader import load_stack_config

        try:
            stored = load_stack_config().tools.get(config_cls.tool_name, {})
        except FileNotFoundError:
            stored = {}
        default = config_cls.model_fields[field_name].default
        name = stored.get(field_name) or (default if isinstance(default, str) else field_name)

        from ..resolver import Resolver

        try:
            resolver = Resolver.from_active_config()
            resolved = resolver.resolve_database(name)
        except Exception:
            pytest.skip(_skip_message(name))
        connection_name = resolved.connection.name
        if not resolver.config.connections[connection_name].test_only:
            pytest.fail(_not_test_only_message(name, connection_name))
        return resolved

    @staticmethod
    def _require_test_only_engine(engine: Engine) -> None:
        """Refuse to use *engine* with ``isolated_test_schema()`` unless it
        matches a ``test_only=true`` connection in the active config.

        Unlike ``isolated_database()``, this path is handed an already-built
        engine directly, not a config field name, so there's nothing to
        resolve through ``_resolve_and_check()``. The engine's own URL is
        matched against ``config.connections`` by host/database/port
        instead, applying the same safety posture: refuse by default,
        require a positive, known, test_only match before creating a real,
        committed schema.
        """
        from ..loader import load_stack_config

        url = engine.url
        safe_url = url.render_as_string(hide_password=True)
        try:
            config = load_stack_config()
        except FileNotFoundError:
            pytest.fail(_unknown_engine_message(safe_url))

        match = next(
            (
                name
                for name, conn in config.connections.items()
                if conn.host == url.host
                and conn.database_name == url.database
                and conn.port == url.port
            ),
            None,
        )
        if match is None:
            pytest.fail(_unknown_engine_message(safe_url))
        if not config.connections[match].test_only:
            pytest.fail(_engine_not_test_only_message(match))

    @abstractmethod
    def isolated_database(
        self,
        resolved: "ResolvedDatabase",
        *,
        extensions: Sequence[str] = (),
        **engine_kwargs: object,
    ) -> AbstractContextManager[IsolatedTestDatabase]:
        """Yield an isolated, dialect-appropriate test database resource."""

    @abstractmethod
    def temporary_schema(
        self, engine: Engine, *, prefix: str = "test"
    ) -> AbstractContextManager[str]:
        """Yield a uniquely-named, genuinely-committed schema, dropped on exit.
        The schema itself is created empty. Filling it with data is the
        caller's job, done inside the ``with`` block, using its own
        connection. The point is that this is a real commit, not a
        rollback-only transaction, so a separate connection can see it too.
        That separate connection is normally not the test's own, but one
        built internally by the code under test.

        Notes
        -----
        Not the default path - see ``isolated_database()`` for that. 
        """
