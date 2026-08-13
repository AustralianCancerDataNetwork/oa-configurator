"""Resolution helpers: turn logical config names into typed, usable handles."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, TypeVar, get_args, get_origin

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined
from sqlalchemy.engine import Engine

from .domains.llm.schema import (
    ModelConfig,
    ProviderConfig,
    ResolvedModel,
    ResolvedProvider,
)
from .domains.resources.schema import (
    ConnectionConfig,
    DatabaseConfig,
    DatabaseEntry,
    ResolvedConnection,
    ResolvedDatabase,
)
from .domains.vector_stores.schema import ResolvedVectorStore, VectorStoreConfig
from .stack_config import (
    StackConfig,
    _ref_section,
    mismatched_kind_refs,
    unresolved_refs,
)
from .package_base import ConfigurationError, PackageConfigBase
from .refs import RefTo, _iter_refs, is_sensitive

T = TypeVar("T")
TConfig = TypeVar("TConfig", bound=PackageConfigBase)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedToolConfig:
    """Resolved tool section with raw extra dict for PackageConfigBase consumption."""

    name: str
    extra: dict[str, Any]

    def __repr__(self) -> str:
        return (
            f"ResolvedToolConfig(name={self.name!r}, extra_keys={sorted(self.extra)!r})"
        )


# ------------------
# Generic recursive resolution machinery: reuse an existing entry, or create
# one on the spot, recursing into any RefTo field the entry's own schema has
# (e.g. a new database recurses into resolving/creating its connection).
# It is domain-agnostic, shared by cli.py's interactive commands and, via
# PackageConfigBase.run_configure, package-field configuration alike.
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


def _is_flag_settable(info: Any) -> bool:
    """Whether a field can be set via a CLI flag, ``--set`` value, or
    interactive prompt/confirm at all.

    Excludes dict/list fields (e.g. ModelConfig.configuration, a
    free-form escape hatch with no sensible single-flag or single-line
    representation, left to its own default_factory). Bool fields (e.g.
    ConnectionConfig.test_only) ARE flag-settable, either as a
    true/false/yes/no flag value or through an interactive confirm. They
    never go through a free-text prompt, though (see the ``is_bool``
    branch in :func:`_resolve_named_entry`).

    Also excludes a single-member ``Literal`` field, since its one
    legal value is already fixed and can't be anything else.
    """
    origin = get_origin(info.annotation)
    if origin in (dict, list) or info.annotation is dict:
        return False
    if origin is Literal and len(get_args(info.annotation)) == 1:
        return False
    return True


def _flag_name(name: str) -> str:
    """Build a Click flag name from a field name.

    Parameters
    ----------
    name : str
        The field name, e.g. "database_name" or "test_cdm_db". Underscores
        become hyphens.

    Returns
    -------
    str
        The flag name, e.g. "--database-name" or "--test-cdm-db".
    """
    return f"--{name.replace('_', '-')}"


def _check_missing_required(
    display_name: str,
    missing_required: list[str],
    *,
    non_interactive: bool,
    headless: bool = False,
) -> None:
    """Abort with a clear error, naming exact CLI flags, if fields are missing.

    A dotted name (e.g. ``"cdm_db.connection"``, produced by the nested
    ``--set`` creation described in :func:`_resolve_nested_flag_value`)
    is reported as a ``--set path=value`` hint instead of a plain flag,
    since no such flag exists.
    """
    if not non_interactive or not missing_required:
        return
    if headless:
        fields = ", ".join(missing_required)
        raise ConfigurationError(
            f"Missing required field(s) for {display_name}: {fields}"
        )
    import typer
    from rich.console import Console

    err_console = Console(stderr=True)
    hints = ", ".join(
        f"--set {k}=<value>" if "." in k else _flag_name(k) for k in missing_required
    )
    err_console.print(
        f"\n[red bold]Missing required field(s) for {display_name!r}:[/red bold] {hints}\n"
        f"No flag or stored config is available for these. Pass them explicitly."
    )
    raise typer.Exit(1)


def _is_test_marked(name: str, target: type[BaseModel], config: StackConfig) -> bool:
    """Whether an existing entry is marked test-only, checked recursively
    through its primary RefTo field. Walks the entry's own runtime type,
    not the caller-supplied target.

    Notes
    -----
    Follows only the first-declared RefTo field (pydantic preserves
    declaration order), not every RefTo field the type has: a
    CDMDatabaseConfig's primary `connection` decides test-ness, matching
    what Resolver.resolve_package_config enforces. A secondary reference
    like `vocab_connection` is deliberately not consulted, so this has one
    single, consistent definition of "is this test" rather than two.
    """
    entry = getattr(config, _ref_section(target)).get(name)
    if entry is None:
        return False
    if isinstance(entry, ConnectionConfig):
        return entry.test_only
    primary = next(iter(_iter_refs(type(entry))), None)
    if primary is None:
        return False
    field_name, ref = primary
    value = getattr(entry, field_name)
    return value is not None and _is_test_marked(value, ref.target, config)


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
    existing entry in target's section, or create one on the spot, recursing
    into any RefTo fields *target* itself has.
    """
    import typer
    from rich.console import Console
    from rich.markup import escape

    console = Console()
    err_console = Console(stderr=True)

    section = _ref_section(target, field_name=field_name)
    section_dict: dict[str, Any] = getattr(config, section)
    candidates = sorted(
        n for n in section_dict if _is_test_marked(n, target, config) == is_test
    )

    console.print(f"\n[bold]{escape(field_name)}[/bold]")
    if description:
        console.print(f"[dim]{escape(description)}[/dim]")

    if candidates:
        console.print(f"  Configured {section}: {', '.join(candidates)}")
        suggested = default_name if default_name in candidates else candidates[0]
        choice = typer.prompt(
            "  Point to an existing entry, or 'new' to create one", default=suggested
        )
        if choice != "new" and choice in candidates:
            return choice
        name = (
            typer.prompt(f"  New {section[:-1]} name", default=default_name)
            if choice == "new"
            else choice
        )
    else:
        console.print(f"  No {section} configured yet.")
        name = typer.prompt(f"  New {section[:-1]} name", default=default_name)

    if name not in section_dict:
        section_dict[name] = _resolve_named_entry(
            target,
            config,
            flags=None,
            existing=None,
            missing_required=[],
            name_hint=name,
            is_test=is_test,
        )
    elif is_test != _is_test_marked(name, target, config):
        if is_test:
            err_console.print(
                f"\n[red bold]DANGER[/red bold]: {name!r} is not marked test_only=true. "
                f"Point test databases only to test_only connections.\n"
            )
        else:
            err_console.print(
                f"\n[red bold]DANGER[/red bold]: {name!r} is marked test_only=true. "
                f"Point production fields only at non-test_only connections.\n"
            )
        raise typer.Exit(1)
    return name


def _find_production_collision(
    host: str | None, database_name: str | None, port: int | None, config: StackConfig
) -> str | None:
    """Return the name of a non-test_only connection matching host/database
    name/port, or None. Checks config.connections directly, independent of
    whether/how a database entry references it."""
    for conn_name, conn in config.connections.items():
        if conn.test_only:
            continue
        if (
            conn.host == host
            and conn.database_name == database_name
            and conn.port == port
        ):
            return conn_name
    return None


def _abort_on_invalid_entry(
    target: type[BaseModel], exc: ValidationError, *, headless: bool = False
) -> NoReturn:
    """Abort with a clear error when a schema rejects the assembled entry.

    Constructing the entry is the last thing :func:`_resolve_named_entry`
    does, and a schema can still refuse it there: a field-level constraint,
    or a cross-field ``model_validator`` (e.g. ``ModelConfig`` rejecting
    ``embedding_dim`` on a model that doesn't declare ``embeddings``).
    Pydantic's own ``str(exc)`` reports the whole input dict, the error
    type, and a docs URL, which is a traceback aimed at a Python caller,
    not at someone fixing a config entry. Each error becomes one line
    naming the flag to change instead.
    """
    field_problems: list[tuple[str, str]] = []
    for error in exc.errors():
        # A model_validator(mode="after") has no loc: the complaint is about
        # the combination of fields, so there's no single flag to point at.
        location = ".".join(str(part) for part in error["loc"])
        message = error["msg"].removeprefix("Value error, ")
        field_problems.append((location, message))

    if headless:
        problems = "; ".join(
            f"{location}: {message}" if location else message
            for location, message in field_problems
        )
        raise ConfigurationError(f"Invalid {target.__name__}: {problems}") from exc

    import typer
    from rich.console import Console

    problems = [
        f"  {_flag_name(location)}: {message}" if location else f"  {message}"
        for location, message in field_problems
    ]

    Console(stderr=True).print(
        f"[red bold]Invalid {target.__name__}:[/red bold]\n" + "\n".join(problems)
    )
    raise typer.Exit(1)


def _check_test_collision(
    new_conn: ConnectionConfig, config: StackConfig, *, headless: bool = False
) -> None:
    """Abort if a new test-only connection's details match a real, non-test one.

    Test databases run DROP SCHEMA CASCADE; pointing one at production data
    by mistake (e.g. copy-pasted host/database name) would destroy it.
    """
    match = _find_production_collision(
        new_conn.host, new_conn.database_name, new_conn.port, config
    )
    if match is not None:
        if headless:
            raise ConfigurationError(
                "Test-only connection details match non-test connection "
                f"{match!r}; use a different host or database name"
            )

        import typer
        from rich.console import Console

        err_console = Console(stderr=True)
        err_console.print(
            f"\n[red bold]DANGER[/red bold]: these connection details match the"
            f" non-test connection [bold]{match!r}[/bold] (same host, database name, and port).\n"
            f"Tests run DROP SCHEMA CASCADE, which would destroy your data.\n"
            f"Use a different [bold]host[/bold] or [bold]database name[/bold]."
        )
        raise typer.Exit(1)


def _resolve_nested_flag_value(
    field_name: str,
    info: Any,
    nested: RefTo,
    raw: dict[str, str],
    config: StackConfig,
    *,
    name_hint: str | None,
    is_test: bool,
    missing_required: list[str],
    headless: bool = False,
) -> str | None:
    """Resolve a RefTo field's nested ``--set field.subfield=value`` dict
    into a saved entry, returning the name it was saved under.

    Lets a non-interactive caller create a brand-new target (e.g. a
    database and the connection it points at, in the same call) instead of
    requiring the target to already exist. This restores one-shot creation
    of a whole reference chain from a single ``configure`` invocation.

    *raw* may include a ``name`` key to choose the entry's name explicitly.
    Otherwise the field's own default (or *name_hint*, or the field name
    itself) is used, matching the interactive wizard's own naming default.
    An entry already saved under that name is updated rather than replaced
    outright: its stored fields carry over for anything *raw* doesn't
    mention.

    Returns None when the nested entry itself is missing a required field,
    appending dotted ``field_name.subfield`` paths to *missing_required*
    instead. This lets the caller report every missing field from one
    non-interactive call in a single error, at any nesting depth.
    """
    has_default = info.default not in (None, PydanticUndefined)
    section_dict: dict[str, Any] = getattr(
        config, _ref_section(nested.target, field_name=field_name)
    )
    sub_flags = dict(raw)
    name = sub_flags.pop("name", None) or (
        str(info.default) if has_default else (name_hint or field_name)
    )

    nested_missing: list[str] = []
    nested_entry = _resolve_named_entry(
        nested.target,
        config,
        flags=sub_flags,
        existing=section_dict.get(name),
        missing_required=nested_missing,
        name_hint=name,
        is_test=is_test,
        headless=headless,
    )
    if nested_missing:
        missing_required.extend(f"{field_name}.{m}" for m in nested_missing)
        return None
    section_dict[name] = nested_entry
    return name


def _resolve_named_entry(
    target: type[BaseModel],
    config: StackConfig,
    *,
    flags: dict[str, Any] | None,
    existing: BaseModel | None,
    missing_required: list[str],
    name_hint: str | None = None,
    is_test: bool = False,
    headless: bool = False,
) -> BaseModel | None:
    """Resolve one entry of *target*: flag, then stored value, then an
    interactive prompt, recursing into any RefTo field via
    :func:`_resolve_ref` (or a nested ``--set field.subfield=value`` dict,
    see :func:`_resolve_nested_flag_value`). Bool fields use a confirm
    prompt, never free text.

    *name_hint*/*is_test* only apply to the brand-new-entry path (no
    *flags*, no *existing*): the nested-ref default name, and the
    test-only wizard's recursion flag.

    Returns None if a required field is missing non-interactively (check
    *missing_required*), and aborts with ``typer.Exit(1)`` if *flags*
    names a field *target* doesn't have.
    """
    import typer

    non_interactive = flags is not None
    flags = flags or {}
    values: dict[str, Any] = {}

    for field_name, info in target.model_fields.items():
        stored = getattr(existing, field_name, None) if existing is not None else None
        if not _is_flag_settable(info):
            # dict/list fields (e.g. ModelConfig.configuration) carry over
            # an existing value on update. A single-member Literal (e.g.
            # `kind`) never does, as it's a fixed per-class constant, and
            # carrying it over could be invalid for a different target class.
            is_fixed_literal = (
                get_origin(info.annotation) is Literal
                and len(get_args(info.annotation)) == 1
            )
            if stored is not None and not is_fixed_literal:
                values[field_name] = stored
            continue

        is_bool = info.annotation is bool
        nested = _nested_ref(info)
        has_default = info.default not in (None, PydanticUndefined)
        raw = flags.get(field_name)

        if isinstance(raw, dict) and nested is not None:
            resolved_name = _resolve_nested_flag_value(
                field_name,
                info,
                nested,
                raw,
                config,
                name_hint=name_hint,
                is_test=is_test,
                missing_required=missing_required,
                headless=headless,
            )
            if resolved_name is not None:
                values[field_name] = resolved_name
            continue

        if raw is not None:
            if is_bool:
                values[field_name] = str(raw).strip().lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
            else:
                values[field_name] = raw if info.is_required() else (raw or None)
            continue

        if field_name == "test_only" and is_test:
            # Forced by the recursive test-database wizard. Never asked
            # here, and never overridable by a stored or default value.
            values[field_name] = True
            continue

        if is_bool:
            if non_interactive:
                if stored is not None:
                    values[field_name] = stored
                elif has_default:
                    values[field_name] = info.default
                # else: no default and non-interactive -> omit, let pydantic apply it
            else:
                values[field_name] = typer.confirm(
                    info.description or field_name,
                    default=bool(stored)
                    if stored is not None
                    else (bool(info.default) if has_default else False),
                )
            continue

        if nested is not None:
            if not info.is_required():
                if stored is not None:
                    values[field_name] = stored
                # else: optional nested ref stays unset unless a flag/stored value set it above
            elif non_interactive:
                if stored is not None:
                    values[field_name] = stored
                elif not has_default:
                    missing_required.append(field_name)
                else:
                    values[field_name] = info.default
            else:
                default_name = (
                    stored
                    if stored is not None
                    else (str(info.default) if has_default else (name_hint or ""))
                )
                values[field_name] = _resolve_ref(
                    field_name,
                    info.description or "",
                    nested.target,
                    config,
                    default_name=default_name,
                    is_test=is_test,
                )
            continue

        if non_interactive:
            if stored is not None:
                values[field_name] = stored
            elif not has_default and info.is_required():
                missing_required.append(field_name)
            elif has_default:
                values[field_name] = info.default
            # else: field has a default_factory (e.g. dict). Omit it and
            # let pydantic apply its own default.
            continue

        default_value = (
            str(stored)
            if stored is not None
            else (str(info.default) if has_default else "")
        )
        raw_input = typer.prompt(
            info.description or field_name,
            default=default_value,
            hide_input=is_sensitive(info),
        )
        values[field_name] = raw_input if info.is_required() else (raw_input or None)

    if non_interactive:
        unknown = set(flags) - set(target.model_fields)
        if unknown:
            if headless:
                names = ", ".join(sorted(unknown))
                valid = ", ".join(target.model_fields)
                raise ConfigurationError(
                    f"{target.__name__} has no field(s): {names}. Valid fields: {valid}"
                )
            from rich.console import Console

            Console(stderr=True).print(
                f"[red bold]{target.__name__} has no field(s):[/red bold] {', '.join(sorted(unknown))}\n"
                f"Valid fields: {', '.join(target.model_fields)}"
            )
            raise typer.Exit(1)

    if non_interactive and missing_required:
        return None
    try:
        entry = target(**values)
    except ValidationError as exc:
        _abort_on_invalid_entry(target, exc, headless=headless)
    if is_test and isinstance(entry, ConnectionConfig):
        entry.test_only = True
    if isinstance(entry, ConnectionConfig) and entry.test_only:
        _check_test_collision(entry, config, headless=headless)
    return entry


class Resolver:
    """Resolves logical names in a StackConfig into typed, usable handles.

    Thin dispatch: each ``resolve_*`` method looks up the raw config entry
    (via the matching ``get_*``) and delegates to that entry's own
    ``.resolve()`` method. The resolution logic itself lives on the
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
        """Resolve a database name to a concrete connection and effective schema.

        Parameters
        ----------
        name : str
            Database name as declared in ``[databases]``.

        Returns
        -------
        ResolvedDatabase
            Fully resolved database. Returns a :class:`ResolvedCDMDatabase` for a
            ``kind="cdm"`` entry, or a plain :class:`ResolvedDatabase` for ``kind="generic"``.

        Raises
        ------
        KeyError
            If *name* does not exist in the config.
        """
        resolved = self.get_database(name).resolve(name, self.config)
        logger.debug(
            "Resolved database %r → connection=%s schema_name=%r",
            name,
            resolved.connection.safe_url,
            resolved.schema_name,
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
            "Resolved model %r → provider=%s model=%r",
            name,
            resolved.provider.provider,
            resolved.model,
        )
        return resolved

    def resolve_vector_store(self, name: str) -> ResolvedVectorStore:
        """Resolve a vector store name to a concrete, backend-ready configuration.

        The unit consuming packages use directly: a package's own config
        just names an entry here (e.g. ``vector_store_name = "vector_store"``).

        Parameters
        ----------
        name : str
            Vector store name as declared in ``[vector_stores]``.

        Returns
        -------
        ResolvedVectorStore
            Fully resolved vector store, with a concrete database when
            configured.

        Raises
        ------
        KeyError
            If *name* (or its database) does not exist in the config.
        """
        resolved = self.get_vector_store(name).resolve(name, self.config)
        logger.debug(
            "Resolved vector store %r → backend_type=%r", name, resolved.backend_type
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
        ``cdm_db``/``embedding_model_name``) resolves to a configured entry.
        Unlike :meth:`resolve_tool`, a missing tool section itself is not
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
            If a ``RefTo``-marked field names an entry that doesn't exist,
            names an entry of the wrong concrete subtype (e.g. a
            ``RefTo(CDMDatabaseConfig)`` field pointing at a
            ``GenericDatabaseConfig`` entry), or names a database whose
            connection's ``test_only`` flag disagrees with the field's own
            ``is_test`` declaration (a production field pointed at a test
            connection, or vice versa).
        """
        tool = self.config.tools.get(cls.tool_name)
        instance = cls.model_validate(tool if tool is not None else {})

        for field_name, value, section in unresolved_refs(instance, self.config):
            raise ConfigurationError(
                f"[tools.{cls.tool_name}].{field_name} references unknown "
                f"{section[:-1]} {value!r}.\n"
                f"Run 'omop-config configure {cls.tool_name}' to set it up."
            )

        for field_name, value, expected, actual in mismatched_kind_refs(
            instance, self.config
        ):
            raise ConfigurationError(
                f"[tools.{cls.tool_name}].{field_name} requires a {expected.__name__} entry, but "
                f"{value!r} is a {actual.__name__}.\n"
                f"Run 'omop-config configure {cls.tool_name}' to point it at a matching database."
            )

        for field_name, ref in _iter_refs(cls):
            value = getattr(instance, field_name)
            if value is None:
                continue
            # unresolved_refs/mismatched_kind_refs above already guarantee
            # value exists and is the right concrete type, so the chain
            # walk below always has something valid to recurse through --
            # not just for DatabaseConfig fields, but any RefTo chain that
            # eventually reaches one (e.g. RefTo(VectorStoreConfig) via
            # VectorStoreConfig.database).
            if _is_test_marked(value, ref.target, self.config) != ref.is_test:
                raise ConfigurationError(
                    f"[tools.{cls.tool_name}].{field_name} is marked is_test={ref.is_test}, but "
                    f"{value!r} does not resolve to a test_only={ref.is_test} connection.\n"
                    "A test field must point at a test_only connection (directly or via a "
                    "nested reference, e.g. a vector store's database), and a non-test field "
                    "must not."
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
        databases: Mapping[str, DatabaseEntry] | None = None,
        providers: dict[str, ProviderConfig] | None = None,
        models: dict[str, ModelConfig] | None = None,
        vector_stores: dict[str, VectorStoreConfig] | None = None,
        tools: dict[str, dict[str, Any]] | None = None,
    ) -> Resolver:
        """Return a new Resolver with entries merged over the current config.

        Useful for session-level overrides without touching the TOML file.

        Parameters
        ----------
        connections : dict[str, ConnectionConfig], optional
            Connection entries, keyed by name, merged over the current config.
        databases : Mapping[str, DatabaseEntry], optional
            Database entries, keyed by name, merged over the current config.
            ``Mapping`` so a caller can pass just one concrete
            kind (e.g. ``dict[str, GenericDatabaseConfig]``) without a
            dict-invariance error.
        providers : dict[str, ProviderConfig], optional
            Provider entries, keyed by name, merged over the current config.
        models : dict[str, ModelConfig], optional
            Model entries, keyed by name, merged over the current config.
        vector_stores : dict[str, VectorStoreConfig], optional
            Vector-store entries, keyed by name, merged over the current config.
        tools : dict[str, dict[str, Any]], optional
            Per-package ``[tools.<name>]`` sections, keyed by tool name,
            merged over the current config.
        """
        new_config = StackConfig(
            connections={**self.config.connections, **(connections or {})},
            databases={**self.config.databases, **(databases or {})},
            providers={**self.config.providers, **(providers or {})},
            models={**self.config.models, **(models or {})},
            vector_stores={**self.config.vector_stores, **(vector_stores or {})},
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

    def vector_store_names(self) -> tuple[str, ...]:
        """Return a sorted tuple of configured vector store names."""
        return self.config.vector_store_names()

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

    def get_vector_store(self, name: str) -> VectorStoreConfig:
        """Return the raw VectorStoreConfig for a vector store name.

        Raises
        ------
        KeyError
            If *name* does not exist in the config.
        """
        return _get_named(self.config.vector_stores, "vector store", name)

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
            f"vector_stores={len(self.config.vector_stores)}, "
            f"tools={len(self.config.tools)})"
        )


def _get_named(mapping: dict[str, T], kind: str, name: str) -> T:
    try:
        return mapping[name]
    except KeyError as exc:
        raise KeyError(f"Unknown {kind}: {name!r}") from exc
