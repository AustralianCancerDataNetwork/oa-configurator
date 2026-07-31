"""Resolution helpers: turn logical config names into typed, usable handles."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TypeVar, get_origin

from pydantic import BaseModel
from pydantic_core import PydanticUndefined
from sqlalchemy.engine import Engine

from .domains.llm.schema import ModelConfig, ProviderConfig, ResolvedModel, ResolvedProvider
from .domains.resources.schema import (
    ConnectionConfig,
    DatabaseConfig,
    ResolvedConnection,
    ResolvedDatabase,
    Role,
)
from .stack_config import _REF_SECTIONS, StackConfig, unresolved_refs
from .package_base import ConfigurationError, PackageConfigBase
from .refs import RefTo, _iter_refs, is_sensitive

__all__ = [
    "ConfigurationError",
    "ConnectionConfig",
    "DatabaseConfig",
    "ModelConfig",
    "ProviderConfig",
    "Resolver",
    "ResolvedConnection",
    "ResolvedDatabase",
    "ResolvedModel",
    "ResolvedProvider",
    "ResolvedToolConfig",
    "Role",
]

T = TypeVar("T")
TConfig = TypeVar("TConfig", bound=PackageConfigBase)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedToolConfig:
    """Resolved tool section with raw extra dict for PackageConfigBase consumption."""

    name: str
    extra: dict[str, Any]

    def __repr__(self) -> str:
        return f"ResolvedToolConfig(name={self.name!r}, extra_keys={sorted(self.extra)!r})"


# ------------------
# Generic recursive resolution machinery: reuse an existing entry, or create
# one on the spot, recursing into any RefTo field the entry's own schema has
# (e.g. a new database recurses into resolving/creating its connection).
# Domain-agnostic -- shared by cli.py's interactive commands and (via
# PackageConfigBase.run_configure) package-field configuration alike.
# typer/rich are already unconditional oa-configurator dependencies (see
# pyproject.toml); imported lazily inside each function body regardless, so
# a consumer that only ever calls resolve_database()/get_engine() doesn't
# pay to load them.
# ------------------

def _nested_ref(info: Any) -> RefTo | None:
    """Return the RefTo marker on a FieldInfo, if it has one."""
    for marker in info.metadata:
        if isinstance(marker, RefTo):
            return marker
    return None


def _is_promptable(info: Any) -> bool:
    """Whether a field is something the wizard should ask about at all.

    Excludes bool fields (e.g. ConnectionConfig.test_only -- programmatic
    only, never something a user picks by typing True/False) and dict/other
    non-scalar fields (e.g. ModelConfig.configuration -- a free-form escape
    hatch with no sensible single-line prompt; left to its own
    default_factory).
    """
    return get_origin(info.annotation) not in (dict, list) and info.annotation not in (bool, dict)


def _is_test_marked(name: str, target: type[BaseModel], config: StackConfig) -> bool:
    """Whether an existing entry is marked test-only, checked recursively
    through any RefTo field it has (e.g. a database is test-only iff the
    connection it points to is)."""
    entry = getattr(config, _REF_SECTIONS[target]).get(name)
    if entry is None:
        return False
    if isinstance(entry, ConnectionConfig):
        return entry.test_only
    return any(
        (value := getattr(entry, field_name)) is not None and _is_test_marked(value, ref.target, config)
        for field_name, ref in _iter_refs(target)
    )


def _resolve_ref(
    field_name: str,
    description: str,
    target: type[BaseModel],
    config: StackConfig,
    *,
    default_name: str,
    is_test: bool = False,
) -> str | None:
    """Resolve a RefTo(target)-marked field interactively: offer reuse of an
    existing entry in target's section, or create one on the spot -- recursing
    into any RefTo fields *target* itself has.
    """
    import typer
    from rich.console import Console
    from rich.markup import escape

    console = Console()
    err_console = Console(stderr=True)

    section = _REF_SECTIONS[target]
    section_dict: dict[str, Any] = getattr(config, section)
    candidates = sorted(n for n in section_dict if _is_test_marked(n, target, config) == is_test)

    console.print(f"\n[bold]{escape(field_name)}[/bold]")
    if description:
        console.print(f"[dim]{escape(description)}[/dim]")

    if candidates:
        console.print(f"  Configured {section}: {', '.join(candidates)}")
        suggested = default_name if default_name in candidates else candidates[0]
        choice = typer.prompt("  Point to an existing entry, or 'new' to create one", default=suggested)
        if choice != "new" and choice in candidates:
            return choice
        name = typer.prompt(f"  New {section[:-1]} name", default=default_name) if choice == "new" else choice
    else:
        console.print(f"  No {section} configured yet.")
        name = typer.prompt(f"  New {section[:-1]} name", default=default_name)

    if name not in section_dict:
        section_dict[name] = _resolve_named_entry(
            target, config, flags=None, existing=None, missing_required=[],
            name_hint=name, is_test=is_test,
        )
    elif is_test != _is_test_marked(name, target, config):
        err_console.print(
            f"\n[red bold]DANGER[/red bold]: {name!r} is not marked test_only=true. "
            f"Point test databases only to test_only connections.\n"
        )
        raise typer.Exit(1)
    return name


def _check_test_collision(new_conn: ConnectionConfig, config: StackConfig) -> None:
    """Abort if a new test-only connection's details match a real, non-test one.

    Test databases run DROP SCHEMA CASCADE; pointing one at production data
    by mistake (e.g. copy-pasted host/database name) would destroy it.
    """
    import typer
    from rich.console import Console

    err_console = Console(stderr=True)
    for db_name, db in config.databases.items():
        conn = config.connections.get(db.connection)
        if conn is None or conn.test_only:
            continue
        if conn.host == new_conn.host and conn.database_name == new_conn.database_name and conn.port == new_conn.port:
            err_console.print(
                f"\n[red bold]DANGER[/red bold]: these connection details match the"
                f" [bold]{db_name!r}[/bold] database (same host and database name).\n"
                f"Tests run DROP SCHEMA CASCADE, which would destroy your data.\n"
                f"Use a different [bold]host[/bold] or [bold]database name[/bold]."
            )
            raise typer.Exit(1)


def _resolve_named_entry(
    target: type[BaseModel],
    config: StackConfig,
    *,
    flags: dict[str, str] | None,
    existing: BaseModel | None,
    missing_required: list[str],
    name_hint: str | None = None,
    is_test: bool = False,
) -> BaseModel | None:
    """Resolve one entry of *target*: flag, then stored value (from
    *existing*), then -- interactively -- a prompt, recursing into any
    RefTo field via :func:`_resolve_ref`. Non-interactively, an unresolved
    RefTo field just uses its own default; existence is validated by
    ``StackConfig``'s cross-reference check once the entry is saved.

    *name_hint* and *is_test* only matter for the brand-new-entry path (no
    *flags*, no *existing*): *name_hint* is the fallback default offered
    when recursing into a required nested ref with no default of its own
    (e.g. a newly-named database recursing into naming its connection);
    *is_test* propagates the test-only wizard's "keep this in the test-only
    pool" choice into that same recursion, and marks a freshly-created
    :class:`ConnectionConfig` as ``test_only``.

    Returns None (instead of constructing *target*, which could raise) when
    a required field is missing non-interactively -- the caller is expected
    to check *missing_required* (e.g. via ``_check_missing_required``)
    before using the result.
    """
    import typer

    non_interactive = flags is not None
    flags = flags or {}
    values: dict[str, Any] = {}

    for field_name, info in target.model_fields.items():
        stored = getattr(existing, field_name, None) if existing is not None else None
        if not _is_promptable(info):
            # bool/dict/etc: programmatic-only, never interactively prompted --
            # but an existing value (e.g. ModelConfig.configuration) is still
            # carried over on update; otherwise leave unset for pydantic's
            # own default/default_factory to apply.
            if stored is not None:
                values[field_name] = stored
            continue
        nested = _nested_ref(info)
        has_default = info.default not in (None, PydanticUndefined)

        if (raw := flags.get(field_name)) is not None:
            values[field_name] = raw if info.is_required() else (raw or None)
        elif stored is not None:
            values[field_name] = stored
        elif nested is not None:
            if not info.is_required():
                pass  # optional nested ref: stays unset unless a flag/stored value set it above
            elif non_interactive:
                if not has_default:
                    missing_required.append(field_name)
                else:
                    values[field_name] = info.default
            else:
                default_name = str(info.default) if has_default else (name_hint or "")
                values[field_name] = _resolve_ref(
                    field_name, info.description or "", nested.target, config,
                    default_name=default_name, is_test=is_test,
                )
        elif non_interactive:
            if not has_default and info.is_required():
                missing_required.append(field_name)
            elif has_default:
                values[field_name] = info.default
            # else: field has a default_factory (e.g. dict) -- omit, let pydantic apply it
        else:
            default_value = str(info.default) if has_default else ""
            raw = typer.prompt(info.description or field_name, default=default_value, hide_input=is_sensitive(info))
            values[field_name] = raw if info.is_required() else (raw or None)

    if non_interactive and missing_required:
        return None
    entry = target(**values)
    if is_test and isinstance(entry, ConnectionConfig):
        entry.test_only = True
        _check_test_collision(entry, config)
    return entry


class Resolver:
    """Resolves logical names in a StackConfig into typed, usable handles.

    Thin dispatch: each ``resolve_*`` method looks up the raw config entry
    (via the matching ``get_*``) and delegates to that entry's own
    ``.resolve()`` method -- the resolution logic itself lives on the
    schema classes in :mod:`oa_configurator.domains`, next to the data it
    resolves.
    """

    def __init__(self, config: StackConfig) -> None:
        self.config = config

    def resolve_connection(self, name: str) -> ResolvedConnection:
        """Resolve a connection name to a concrete target."""
        target = self.get_connection(name).resolve(name)
        logger.debug("Resolved connection %r → %s", name, target.safe_url)
        return target

    def resolve_provider(self, name: str) -> ResolvedProvider:
        """Resolve a provider name to a concrete connection.

        Parameters
        ----------
        name : str
            Provider name as declared in ``[providers]``.

        Returns
        -------
        ResolvedProvider
            Resolved provider connection.

        Raises
        ------
        KeyError
            If *name* does not exist in the config.
        """
        resolved = self.get_provider(name).resolve(name)
        logger.debug("Resolved provider %r → %s", name, resolved.provider)
        return resolved

    def resolve_database(self, name: str) -> ResolvedDatabase:
        """Resolve a database name to a concrete bundle of connections and schemas.

        The vocab connection falls back to the primary connection when not
        explicitly configured; the vocab schema falls back to the CDM
        schema under the same condition.

        Parameters
        ----------
        name : str
            Database name as declared in ``[databases]``.

        Returns
        -------
        ResolvedDatabase
            Fully resolved database with concrete connection targets and
            effective schema names.

        Raises
        ------
        KeyError
            If *name* does not exist in the config.
        """
        resolved = self.get_database(name).resolve(name, self.config)
        logger.debug(
            "Resolved database %r → connection=%s cdm_schema=%r",
            name,
            resolved.connection.safe_url,
            resolved.cdm_schema,
        )
        return resolved

    def resolve_model(self, name: str) -> ResolvedModel:
        """Resolve a model name to a concrete, backend-ready configuration.

        The unit consuming packages use directly: a package's own config
        just names an entry here (e.g. ``embedding_model_name = "embed-default"``).

        Parameters
        ----------
        name : str
            Model name as declared in ``[models]``.

        Returns
        -------
        ResolvedModel
            Fully resolved model with a concrete provider connection.

        Raises
        ------
        KeyError
            If *name* (or its provider) does not exist in the config.
        """
        resolved = self.get_model(name).resolve(name, self.config)
        logger.debug(
            "Resolved model %r → provider=%s model=%r", name, resolved.provider.provider, resolved.model
        )
        return resolved

    def resolve_tool(self, name: str) -> ResolvedToolConfig:
        """Resolve a tool name to its configuration.

        Parameters
        ----------
        name : str
            Tool name as declared in ``[tools]``.

        Returns
        -------
        ResolvedToolConfig
            Resolved config with the raw extra dict intact. Prefer
            :meth:`resolve_package_config` for the typed, validated equivalent.

        Raises
        ------
        KeyError
            If *name* does not exist in the config.
        """
        tool = self.get_tool(name)
        resolved = ResolvedToolConfig(name=name, extra=dict(tool))
        logger.debug("Resolved tool %r with %d extra key(s)", name, len(resolved.extra))
        return resolved

    def resolve_package_config(self, cls: type[TConfig]) -> TConfig:
        """Resolve and validate a package's own typed ``[tools.<name>]`` section.

        Validates every ``RefTo``-marked field (e.g. a package's own
        ``cdm_db``/``embedding_model_name``) resolves to a configured entry
        -- unlike :meth:`resolve_tool`, a missing tool section itself is not
        an error here, since a package's own fields may all have usable
        defaults even before ``omop-config configure`` has been run.

        Parameters
        ----------
        cls : type[PackageConfigBase]
            The package's ``PackageConfigBase`` subclass to resolve.

        Returns
        -------
        PackageConfigBase
            An instance of *cls* validated from its ``[tools.<name>]``
            section (or an empty dict, if not yet configured).

        Raises
        ------
        ConfigurationError
            If a ``RefTo``-marked field names an entry that doesn't exist.
        """
        tool = self.config.tools.get(cls.tool_name)
        instance = cls.model_validate(tool if tool is not None else {})

        for field_name, value, section in unresolved_refs(instance, self.config):
            raise ConfigurationError(
                f"{cls.__name__}.{field_name} references unknown {section[:-1]} {value!r}.\n"
                f"Run 'omop-config configure {cls.tool_name}' to set it up."
            )

        return instance

    def resolve_engine(self, database: str, **kwargs: Any) -> Engine:
        """Resolve a database name into an engine.

        Parameters
        ----------
        database : str
            The database name to resolve. No zero-argument defaulting: the
            database must be named explicitly.
        **kwargs
            Forwarded to :meth:`ResolvedDatabase.create_engine`.

        Returns
        -------
        sqlalchemy.engine.Engine
        """
        return self.resolve_database(database).create_engine(**kwargs)

    def with_overrides(
        self,
        *,
        connections: dict[str, ConnectionConfig] | None = None,
        databases: dict[str, DatabaseConfig] | None = None,
        providers: dict[str, ProviderConfig] | None = None,
        models: dict[str, ModelConfig] | None = None,
        tools: dict[str, dict[str, Any]] | None = None,
    ) -> Resolver:
        """Return a new Resolver with entries merged over the current config.

        Useful for session-level overrides without touching the TOML file.
        """
        new_config = StackConfig(
            connections={**self.config.connections, **(connections or {})},
            databases={**self.config.databases, **(databases or {})},
            providers={**self.config.providers, **(providers or {})},
            models={**self.config.models, **(models or {})},
            tools={**self.config.tools, **(tools or {})},
            logging=self.config.logging,
        )
        if self.config.loaded_path is not None:
            new_config.bind_loaded_path(self.config.loaded_path)
        return Resolver(new_config)

    def connection_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured connection names."""
        return self.config.connection_names()

    def database_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured database names."""
        return self.config.database_names()

    def provider_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured provider names."""
        return self.config.provider_names()

    def model_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured model names."""
        return self.config.model_names()

    def tool_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured tool names."""
        return self.config.tool_names()

    def get_connection(self, name: str) -> ConnectionConfig:
        """Return the raw ConnectionConfig for a connection name.

        Unlike ``resolve_connection``, this returns the raw config object
        rather than a resolved target; useful when individual fields (host,
        port, etc.) are needed directly.

        Raises
        ------
        KeyError
            If *name* does not exist in the config.
        """
        return _get_named(self.config.connections, "connection", name)

    def get_database(self, name: str) -> DatabaseConfig:
        """Return the raw DatabaseConfig for a database name.

        Raises
        ------
        KeyError
            If *name* does not exist in the config.
        """
        return _get_named(self.config.databases, "database", name)

    def get_provider(self, name: str) -> ProviderConfig:
        """Return the raw ProviderConfig for a provider name.

        Unlike ``resolve_provider``, this returns the raw config object
        rather than a resolved target.

        Raises
        ------
        KeyError
            If *name* does not exist in the config.
        """
        return _get_named(self.config.providers, "provider", name)

    def get_model(self, name: str) -> ModelConfig:
        """Return the raw ModelConfig for a model name.

        Raises
        ------
        KeyError
            If *name* does not exist in the config.
        """
        return _get_named(self.config.models, "model", name)

    def get_tool(self, name: str) -> dict[str, Any]:
        """Return the raw ``[tools.<name>]`` dict for a tool name.

        Raises
        ------
        KeyError
            If *name* does not exist in the config.
        """
        return _get_named(self.config.tools, "tool", name)

    @classmethod
    def from_active_config(cls) -> Resolver:
        """Create a Resolver from the currently active stack config file."""
        from .loader import load_stack_config
        return cls(load_stack_config())

    def __repr__(self) -> str:
        return (
            f"Resolver("
            f"connections={len(self.config.connections)}, "
            f"databases={len(self.config.databases)}, "
            f"providers={len(self.config.providers)}, "
            f"models={len(self.config.models)}, "
            f"tools={len(self.config.tools)})"
        )


def _get_named(mapping: dict[str, T], kind: str, name: str) -> T:
    try:
        return mapping[name]
    except KeyError as exc:
        raise KeyError(f"Unknown {kind}: {name!r}") from exc
