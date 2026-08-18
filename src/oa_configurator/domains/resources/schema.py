"""Resources domain: physical connections and logical CDM/vocab/results database bundles."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import URL, Engine
import sqlalchemy as sa

from ...refs import RefTo, Secret

if TYPE_CHECKING:
    from ...stack_config import StackConfig


class Role(str, Enum):
    """Which physical target a database's logical role maps to.

    Shared between :meth:`ResolvedDatabase.connection_target`, which picks
    a concrete connection (only PRIMARY/VOCAB apply; RESULTS has no
    connection of its own), and :meth:`ResolvedDatabase.schema_translate_map`,
    which picks a schema name (all three apply). One enum instead of two
    separately-typed, overlapping string sets.
    """

    PRIMARY = "primary"
    VOCAB = "vocab"
    RESULTS = "results"


class ConnectionConfig(BaseModel):
    """Complete specification of one physical database connection: server
    address, credentials, and target database.

    Referenced by :attr:`DatabaseConfig.connection` and
    :attr:`DatabaseConfig.vocab_connection`. Each entry under
    ``[connections]`` in ``config.toml`` maps to one instance of this model.

    Passwords are stored in plaintext for now; secret management support
    is planned for a future release.
    """

    model_config = ConfigDict(extra="forbid")

    dialect: str = Field(
        description="SQLAlchemy dialect string, e.g. 'postgresql+psycopg', 'mssql+pyodbc', 'sqlite'."
    )
    host: str | None = Field(
        default=None,
        description=(
            "Hostname or IP address. Required for every dialect except SQLite, which "
            "connects to a local file and has no host to speak of."
        ),
    )
    port: int | None = Field(default=None, description="Port number.")
    user: str | None = Field(default=None, description="Database username.")
    password: Secret = Field(
        default=None,
        description="Plaintext password. Secret management support is planned for a future release.",
    )
    database_name: str | None = Field(
        default=None,
        description=(
            "Database name on the server. Required for SQLite, which has no implicit "
            "default: pass an absolute path, or ':memory:' for an in-memory database."
        ),
    )
    test_only: bool = Field(
        default=False,
        description=(
            "Marks this connection as intended for testing only. "
            "It will be excluded from production database prompts and "
            "used as a safety check to prevent accidental test operations "
            "on production data."
        ),
    )

    def to_env_pairs(self, prefix: str) -> list[str]:
        """Return ``PREFIX_FIELD=value`` strings for each non-None field.

        Used by :func:`~oa_configurator.io.write_env_file` to emit env vars for
        Docker Compose ``env_file:``. Field names are uppercased directly
        (e.g. ``host`` → ``PREFIX_HOST``), so adding a new field here
        automatically appears in the export without touching ``io.py``.
        The ``test_only`` config-only flag is excluded, since it is not a
        database connection parameter.
        """
        return [
            f"{prefix}_{k.upper()}={v}"
            for k, v in self.model_dump().items()
            if v is not None and k != "test_only"
        ]

    def _build_url_obj(self) -> URL:
        if self.dialect.startswith("sqlite"):
            if not self.database_name:
                raise ValueError(
                    "ConnectionConfig has no `database_name` set for a sqlite dialect and no"
                    " longer defaults to ':memory:'. Set `database_name` explicitly -- pass"
                    " ':memory:' if that's actually what you want."
                )
            return URL.create(drivername=self.dialect, database=self.database_name)
        if not self.host:
            raise ValueError(
                "ConnectionConfig has no `host` set and no longer defaults to 'localhost'."
                " Set `host` explicitly in config.toml."
            )
        return URL.create(
            drivername=self.dialect,
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database_name or "",
        )

    def build_url(self) -> str:
        """Build the full connection URL, including the plaintext password.

        Returns
        -------
        str
            SQLAlchemy-compatible connection URL. For SQLite, returns
            ``sqlite:///<database_name>``; ``database_name`` must be set
            explicitly (e.g. to ``:memory:``), there is no implicit default.
        """
        return self._build_url_obj().render_as_string(hide_password=False)

    def safe_url(self) -> str:
        """Build the connection URL with the password redacted.

        Safe for logging and display. Identical to ``build_url()`` for SQLite
        connections, which carry no password.

        Returns
        -------
        str
            Connection URL with ``***`` substituted for the password field.
        """
        return self._build_url_obj().render_as_string(hide_password=True)

    def resolve(self, name: str) -> ResolvedConnection:
        """Resolve this connection to a concrete, engine-ready target."""
        url_obj = self._build_url_obj()
        return ResolvedConnection(
            name=name,
            url=url_obj.render_as_string(hide_password=False),
            safe_url=url_obj.render_as_string(hide_password=True),
            _engine_url=url_obj,
        )


class DatabaseKind(str, Enum):
    """Discriminator for :class:`DatabaseConfig` subclasses."""

    GENERIC = "generic"
    CDM = "cdm"


class DatabaseConfig(BaseModel):
    """Shared interface for every named database: a connection plus a schema
    to route into.

    Abstract in practice: ``kind`` has no default, so every concrete entry
    must declare it explicitly via one of the subclasses below. Use this
    class (not a subclass) for ``isinstance`` checks and ``RefTo`` targets
    that accept any kind; use :data:`DatabaseEntry` for parsing raw config
    data, which dispatches to the correct subclass based on ``kind``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: DatabaseKind = Field(description="Which concrete database shape this entry is.")
    connection: Annotated[str, RefTo(ConnectionConfig)] = Field(
        description="Name of the connection entry (from [connections]) used as the primary server."
    )
    schema_name: str | None = Field(
        default=None,
        description=(
            "Schema this database's tables live in. None means no override, use the "
            "connection's own default/search_path."
        ),
    )

    def resolve(self, name: str, stack: StackConfig) -> ResolvedDatabase:
        """Resolve this database to a concrete connection and effective schema.

        *stack* must already have passed :meth:`StackConfig.validate_references`,
        so ``self.connection`` is guaranteed to exist in ``stack.connections``.
        """
        primary = stack.connections[self.connection].resolve(self.connection)
        return ResolvedDatabase(name=name, connection=primary, schema_name=self.schema_name)


class GenericDatabaseConfig(DatabaseConfig):
    """A connection plus one optional schema. No CDM role-splitting.

    Used by consumers that need a single database with no vocab/results
    distinction, e.g. an embedding store or a metadata database.
    """

    kind: Literal[DatabaseKind.GENERIC] = DatabaseKind.GENERIC  # type: ignore[assignment]


class CDMDatabaseConfig(DatabaseConfig):
    """Maps the OMOP logical roles (CDM, vocab, results) to named connections and schema names.

    This is what a CDM-consuming package treats as "its database": the
    logical CDM/vocab/results bundle, as opposed to :class:`ConnectionConfig`
    (the physical server address and credentials underneath it). Most
    packages only need a single ``cdm_db`` database.
    """

    kind: Literal[DatabaseKind.CDM] = DatabaseKind.CDM  # type: ignore[assignment]
    schema_name: str = Field(
        default="omop",
        description="Schema where CDM clinical tables live.",
    )
    vocab_connection: Annotated[str | None, RefTo(ConnectionConfig)] = Field(
        default=None,
        description="Name of the connection entry for vocabulary tables. Falls back to connection when not set.",
    )
    vocab_schema: str | None = Field(
        default=None,
        description="Vocabulary schema. Falls back to schema_name when not set.",
    )
    results_schema: str | None = Field(
        default=None,
        description="Achilles / Atlas results schema.",
    )

    def resolve(self, name: str, stack: StackConfig) -> ResolvedCDMDatabase:
        """Resolve this database to concrete connections and effective schema names.

        The vocab connection falls back to the primary connection when not
        explicitly configured; the vocab schema falls back to the CDM
        schema under the same condition. *stack* must already have passed
        :meth:`StackConfig.validate_references`, so ``self.connection``/
        ``self.vocab_connection`` are guaranteed to exist in
        ``stack.connections``.
        """
        primary = stack.connections[self.connection].resolve(self.connection)
        vocab_name = self.vocab_connection or self.connection
        vocab = stack.connections[vocab_name].resolve(vocab_name)
        return ResolvedCDMDatabase(
            name=name,
            connection=primary,
            schema_name=self.schema_name,
            vocab_connection=vocab,
            vocab_schema=self.vocab_schema or self.schema_name,
            results_schema=self.results_schema,
        )


DatabaseEntry = Annotated[
    GenericDatabaseConfig | CDMDatabaseConfig,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class ResolvedConnection:
    """Concrete physical connection ready for engine creation.

    Attributes
    ----------
    name : str
        Logical name of the connection as declared in the config.
    url : str
        Full database URL including credentials.
        TODO: make this a private attribute to avoid accidental password exposure;
        requires a factory method or ``__post_init__`` since dataclass field
        visibility can't be changed without breaking callers.
    safe_url : str
        Database URL with credentials redacted, safe for logging and display.
    _engine_url : sqlalchemy.engine.URL
        SQLAlchemy URL object used for engine creation. Avoids the lossy string
        round-trip through ``url`` for SQLite paths containing ``?``/``#``.
    """

    name: str
    url: str
    safe_url: str
    _engine_url: URL = field(repr=False, compare=False)

    def create_engine(self, **kwargs: Any) -> Engine:
        """Create a SQLAlchemy engine for this connection.

        Parameters
        ----------
        **kwargs
            Forwarded to ``sqlalchemy.create_engine``.

        Returns
        -------
        sqlalchemy.engine.Engine
        """
        return sa.create_engine(self._engine_url, **kwargs)

    def __repr__(self) -> str:
        return f"ResolvedConnection(name={self.name!r}, safe_url={self.safe_url!r})"


@dataclass(frozen=True)
class ResolvedDatabase:
    """Resolved generic database: one connection, one optional schema.

    Attributes
    ----------
    name : str
        Logical name of the database as declared in the config.
    connection : ResolvedConnection
        Resolved connection for this database.
    schema_name : str | None
        Effective schema name for this database, or None for no override
        (use the connection's own default/search_path).
    """

    name: str
    connection: ResolvedConnection
    schema_name: str | None

    def create_engine(
        self,
        *,
        execution_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Engine:
        """Create a SQLAlchemy engine with the schema translate map applied.

        Parameters
        ----------
        execution_options : dict, optional
            Additional execution options merged into the engine. The
            ``schema_translate_map`` key is set automatically and must
            not be supplied here.
        **kwargs
            Forwarded to ``sqlalchemy.create_engine``.

        Returns
        -------
        sqlalchemy.engine.Engine
            Engine configured with ``schema_translate_map`` set to
            ``{None: schema_name}``, a genuine no-op when ``schema_name``
            is None, deferring to the connection's own default/search_path.
        """
        engine = self.connection.create_engine(**kwargs)
        merged_opts = dict(execution_options or {})
        merged_opts.setdefault("schema_translate_map", {None: self.schema_name})
        return engine.execution_options(**merged_opts)

    def __repr__(self) -> str:
        return (
            f"ResolvedDatabase(name={self.name!r}, "
            f"connection={self.connection.name!r}, "
            f"schema_name={self.schema_name!r})"
        )


@dataclass(frozen=True)
class ResolvedCDMDatabase(ResolvedDatabase):
    """Resolved CDM database: adds vocab/results role-splitting on top of
    :class:`ResolvedDatabase`.

    Attributes
    ----------
    vocab_connection : ResolvedConnection
        Resolved vocabulary connection for this database. May be the same as
        *connection* if no separate vocab connection is configured.
    vocab_schema : str
        Effective vocabulary schema name for this database. May be the same as
        schema_name if no separate vocab schema is configured.
    results_schema : str | None
        Effective results schema name for this database, or None if not configured.
    """

    vocab_connection: ResolvedConnection
    vocab_schema: str
    results_schema: str | None

    def connection_target(self, role: Role = Role.PRIMARY) -> ResolvedConnection:
        """Return the resolved connection for a given role.

        Parameters
        ----------
        role : Role, optional
            Which connection to return. Defaults to ``Role.PRIMARY``.
            When ``vocab_connection`` was not configured, ``Role.VOCAB`` returns
            the same connection as ``Role.PRIMARY``.

        Returns
        -------
        ResolvedConnection
            The concrete connection for *role*.

        Raises
        ------
        ValueError
            If *role* is ``Role.RESULTS`` (results has no connection of its own).
        """
        if role == Role.PRIMARY:
            return self.connection
        if role == Role.VOCAB:
            return self.vocab_connection
        raise ValueError(f"Role {role!r} has no connection. Valid roles: PRIMARY, VOCAB")

    def schema_translate_map(self) -> dict[str | None, str | None]:
        """SQLAlchemy schema translate map for OMOP ORM models.

        Maps:
          None      → schema_name  (default / unqualified tables → CDM)
          "vocab"   → vocab_schema (or schema_name as fallback)
          "results" → results_schema (omitted when not configured)
        """
        m: dict[str | None, str | None] = {
            None: self.schema_name,
            Role.VOCAB.value: self.vocab_schema,
        }
        if self.results_schema is not None:
            m[Role.RESULTS.value] = self.results_schema
        return m

    def create_engine(
        self,
        role: Role = Role.PRIMARY,
        *,
        execution_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Engine:
        """Create a SQLAlchemy engine with the schema translate map applied.

        The schema translate map routes OMOP ORM models to the correct schemas
        automatically (``None`` -> schema_name, ``"vocab"`` -> vocab_schema,
        ``"results"`` -> results_schema when configured).

        Parameters
        ----------
        role : Role, optional
            Which connection to create an engine for. Defaults to
            ``Role.PRIMARY``.
        execution_options : dict, optional
            Additional execution options merged into the engine. The
            ``schema_translate_map`` key is set automatically and must
            not be supplied here.
        **kwargs
            Forwarded to ``sqlalchemy.create_engine``.

        Returns
        -------
        sqlalchemy.engine.Engine
            Engine configured with ``schema_translate_map`` for OMOP ORM routing.
        """
        engine = self.connection_target(role).create_engine(**kwargs)
        stm = self.schema_translate_map()
        merged_opts = dict(execution_options or {})
        merged_opts.setdefault("schema_translate_map", stm)
        return engine.execution_options(**merged_opts)

    def __repr__(self) -> str:
        return (
            f"ResolvedCDMDatabase(name={self.name!r}, "
            f"connection={self.connection.name!r}, "
            f"schema_name={self.schema_name!r}, "
            f"vocab_schema={self.vocab_schema!r}, "
            f"results_schema={self.results_schema!r})"
        )
