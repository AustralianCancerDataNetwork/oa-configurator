"""Per-dialect strategy for isolated test-database provisioning.

Add a new dialect by subclassing :class:`TestDatabaseStrategy` (two
abstract methods) and registering it in ``testing/__init__.py``'s dispatch
table. No other code changes are needed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Any

import pytest
from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session

from ..domains.resources.sql import connection_key

if TYPE_CHECKING:
    from ..domains.resources.schema import (
        ResolvedConnection,
        ResolvedDatabase,
        SchemaClaim,
    )
    from ..package_base import PackageConfigBase
    from ..resolver import Resolver


class TestDatabaseNotConfigured(Exception):
    """No real configuration is available.

    Raised by ``_resolve_and_check()`` when a config field has no
    resolvable value (rather than skipping immediately, so
    ``isolated_test_database()`` can decide whether to skip or try a
    strategy's ``resolve_without_config()`` fallback first), and by a
    strategy's own ``resolve_without_config()`` when it genuinely needs
    real configuration (the default for anything but an embedded,
    serverless dialect). *field_name* is set only in the first case.

    ``__test__ = False`` tells pytest not to collect this as a test class:
    its name starts with ``Test``, and pytest would otherwise try (and warn)
    the moment any test module imports it as a top-level name.
    """

    __test__ = False

    def __init__(self, message: str = "", *, field_name: str | None = None) -> None:
        super().__init__(message)
        self.field_name = field_name


@dataclass
class IsolatedTestDatabase:
    """An isolated database resource scoped to one test.

    ``.connection`` and ``.session`` share the same underlying transaction.
    Pick whichever fits (Core vs. ORM) for a given test, never mix a
    different one in alongside them.

    ``resolved`` is the ``ResolvedDatabase`` describing this database
    (SQLite: the one built for its own tempfile). Use ``dataclasses.replace(db.resolved, ...)`` rather than
    hand-building a ``ResolvedConnection``/``ResolvedDatabase`` from scratch
    when a test needs one with a field or two overridden.
    """

    connection: Connection
    session: Session
    resolved: "ResolvedDatabase | None" = None

    @property
    def committing_engine(self) -> Engine:
        """Return the real engine for code that must open its own connections.

        Connections opened from this engine are outside the transaction that
        protects ``connection`` and ``session``. Callers must register cleanup
        with the ``cleanup_after_test`` fixture before using this escape hatch.
        """
        return self.connection.engine


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
        config_cls: type["PackageConfigBase"], 
        field_name: str, 
        *, 
        resolver: "Resolver | None" = None
    ) -> "ResolvedDatabase":
        """Resolve *field_name* off *config_cls* and enforce ``test_only``.

        Dialect-agnostic: resolving a config field and checking a
        connection's ``test_only`` flag doesn't depend on Postgres vs.
        SQLite, so this is one shared step every strategy's ``isolated_database()``
        relies on before diverging into dialect-specific work, not
        duplicated per subclass.

        Raises ``TestDatabaseNotConfigured`` (rather than skipping directly) when
        *field_name* has no resolvable value, so a caller with a
        config-free fallback available gets a chance to use it first.

        Parameters
        ----------
        resolver : Resolver, optional
            Use this resolver instead of loading the on-disk active config.
            Lets a consumer inject a session-built ``StackConfig`` instead of
            relying on ``load_stack_config()``/``Resolver.from_active_config()``
            reading the real file.
        Raises
        ------
        ValueError
            If *field_name* is explicitly set in config to a name that isn't
            a ``[databases.*]`` entry. A config-typo must fail loudly, not be
            folded into the same "not configured" skip as a field nobody set.
        """
        if field_name not in config_cls.model_fields:
            raise ValueError(f"{config_cls.__name__} has no field {field_name!r}.")

        from ..resolver import Resolver

        if resolver is None:
            try:
                resolver = Resolver.from_active_config()
            except FileNotFoundError:
                raise TestDatabaseNotConfigured(field_name=field_name) from None

        stored = resolver.config.tools.get(config_cls.tool_name, {})
        default = config_cls.model_fields[field_name].default
        configured_name = stored.get(field_name)
        name = configured_name or (default if isinstance(default, str) else field_name)

        try:
            resolved = resolver.resolve_database(name)
        except KeyError:
            if configured_name is not None:
                raise ValueError(
                    f"{config_cls.tool_name}.{field_name} is set to {configured_name!r}, but "
                    "no such [databases.*] entry exists in the active config. Check for a typo."
                ) from None
            # name fell back to the field's own default/name, which nobody
            # configured explicitly: genuinely "not configured" rather than a typo.
            raise TestDatabaseNotConfigured(field_name=name) from None
        connection_name = resolved.connection.name
        if not resolver.config.connections[connection_name].test_only:
            pytest.fail(_not_test_only_message(name, connection_name))
        return resolved

    def resolve_without_config(self) -> "ResolvedDatabase":
        """Build a ``ResolvedDatabase`` needing no real configuration.

        Raises :class:`TestDatabaseNotConfigured` for any strategy that requires a
        real, resolved connection, the default for everything except an
        embedded, serverless dialect. Override only where this genuinely
        holds.
        """
        raise TestDatabaseNotConfigured(
            f"{type(self).__name__} cannot resolve a test database without configuration."
        )

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

        target = connection_key(url)
        match = next(
            (
                name
                for name, conn in config.connections.items()
                if conn.physical_key() == target
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
        schema_claims: Iterable["SchemaClaim"] = (),
        execution_options: dict[str, Any] | None = None,
        **engine_kwargs: Any,
    ) -> AbstractContextManager[IsolatedTestDatabase]:
        """Yield an isolated, dialect-appropriate test database resource.

        engine_kwargs
            Forwarded to ``resolved.create_engine()``, e.g. ``poolclass`` or
            ``connect_args`` for a caller that needs to tune the underlying
            engine (a session-scoped SQLite engine sharing one real
            connection via ``poolclass=StaticPool``, for example), or
            ``extensions`` for a connect-event callable the engine needs on
            every physical connection (see ``ResolvedDatabase.create_engine``;
            ``install_postgres_extension()`` builds one for a named Postgres
            extension).
        """

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

    @abstractmethod
    def drop_test_database(self, connection: "ResolvedConnection") -> bool:
        """Drop connection's leftover test database, if this dialect has one.

        Only for connections marked ``test_only=true``. Returns True if a
        database was dropped, False if none existed.
        """
