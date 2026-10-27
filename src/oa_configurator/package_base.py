"""Base class for per-package configuration sections.

Each package that wants to support ``omop-config configure <name>`` subclasses
:class:`PackageConfigBase`, declares its typed fields, and registers itself via
the ``omop.config`` entry-point group in its ``pyproject.toml``.

Example
-------
In ``<my-package>/config.py``::

    from typing import Annotated, ClassVar

    from oa_configurator import CDMDatabaseConfig, ModelConfig, PackageConfigBase, RefTo

    class MyPackageConfig(PackageConfigBase):
        tool_name: ClassVar[str] = "<my-package>"
        # A field naming an entry in another section. `omop-config configure`
        # offers to reuse an existing one or create it on the spot, recursing
        # into any RefTo fields the target itself has (e.g. a database's own
        # connection). No separate declaration list: the field IS the
        # declaration. Whether the entry ends up shared with another package
        # is simply a matter of both packages' fields resolving to the same
        # name.
        cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"
        embedding_model_name: Annotated[str, RefTo(ModelConfig)] = "embed-default"
        <additional typed fields here>

    config = MyPackageConfig.get_config()

In ``pyproject.toml``::

    [project.entry-points."omop.config"]
    <my-package> = "<my-package>.config:<MyPackageConfig>"
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import ErrorDetails, PydanticUndefined

from .issues import ConfigIssuesError, ConfigurationError, issues_from_errors
from .refs import SecretSafeBaseModel, describe_errors, is_sensitive, sanitized_errors

if TYPE_CHECKING:
    from .stack_config import StackConfig


class _SanitizedValidationErrorMixin:
    """Shared sanitizing/storage/accessor for exceptions built from a
    pydantic :class:`~pydantic.ValidationError`.

    Both :class:`PackageConfigInvalidError` and
    :class:`StackConfigInvalidError` wrap a validation failure whose
    ``.errors()`` may be pasted into an issue or a CI log: both need the
    input sanitized via :func:`~oa_configurator.refs.sanitized_errors`
    before it touches ``self`` or the exception message, and both expose it
    back the same way. Centralising the call means neither ``__init__`` can
    accidentally store or format the raw, un-sanitized errors instead.

    Notes
    -----
    Neither subclass *is* a :class:`~pydantic.ValidationError` as it is not
    subclassable for this purpose. Its ``__new__`` is Rust-backed and always
    requires ``title``/``line_errors``, which a sanitizing constructor has no
    use for.

    """

    _errors: tuple[ErrorDetails, ...]

    def _sanitize(self, validation_error: ValidationError) -> str:
        """Store the sanitized errors on self and return them rendered as text."""
        self._errors = sanitized_errors(validation_error)
        return describe_errors(self._errors)

    def errors(self) -> list[ErrorDetails]:
        """Return locations and messages without rejected input or context."""
        return list(self._errors)


class PackageConfigInvalidError(_SanitizedValidationErrorMixin, ConfigIssuesError):
    """A package section failed its concrete pydantic schema.

    ``issues`` holds one ``INVALID_ENTRY`` issue per error, with a ``None``
    ``field_path`` for model-level validators. :meth:`errors` exposes the same
    sanitized pydantic details without retaining rejected input or validator
    context.
    """

    def __init__(self, tool_name: str, validation_error: ValidationError) -> None:
        self.tool_name = tool_name
        message = f"Invalid [tools.{tool_name}]: " + self._sanitize(validation_error)
        super().__init__(message, issues_from_errors(self._errors, package=tool_name))


class StackConfigInvalidError(_SanitizedValidationErrorMixin, ConfigurationError):
    """The config file parsed as TOML but failed :class:`StackConfig` validation.

    Sibling of :class:`PackageConfigInvalidError`, sharing its sanitising: a
    stack config holds every connection password and API key in the deployment,
    so this is the error most likely to be pasted into an issue or a CI log.
    The message names the file and the offending field paths and nothing else.

    Attributes
    ----------
    path : pathlib.Path
        The file that failed to validate.
    """

    def __init__(self, path: Path, validation_error: ValidationError) -> None:
        self.path = path
        super().__init__(f"Invalid stack config {path}: " + self._sanitize(validation_error))


def _enter_hint(stored: object, info: FieldInfo) -> str:
    """Return the prompt suffix naming what Enter leaves in effect.

    A sensitive field never shows its value.
    """
    if stored is not None:
        value = stored
    elif info.default is not PydanticUndefined and info.default is not None:
        value = info.default
    else:
        return ""
    if is_sensitive(info):
        return "  [Enter = keep]"
    return f"  [Enter = {value}]"


def _prompt_optional_bool(label: str, hint: str) -> bool | None:
    """Ask a yes/no question where Enter answers ``None`` (leave unchanged)."""
    import click
    import typer

    while True:
        raw = typer.prompt(f"{label} (y/n){hint}", default="", show_default=False).strip()
        if not raw:
            return None
        try:
            return bool(click.BOOL.convert(raw, None, None))
        except click.BadParameter:
            typer.echo("Answer y or n, or press Enter to leave it unchanged.")


class PackageConfigBase(SecretSafeBaseModel):
    """Typed view over a package's ``[tools.<tool_name>]`` TOML section.

    Subclass this and declare typed fields for whatever this package needs.
    A field typed ``Annotated[str, RefTo(CDMDatabaseConfig)]`` (or ``RefTo(GenericDatabaseConfig)``/
    ``RefTo(ModelConfig)``/``RefTo(ProviderConfig)``/``RefTo(ConnectionConfig)``) names an entry in that
    section. ``omop-config configure`` resolves it interactively: reuse an
    existing entry, or create one, recursing into any ``RefTo`` fields the
    target itself has (e.g. a database's own connection).
    :meth:`~oa_configurator.Resolver.resolve_package_config` validates that it
    resolves, raising :exc:`ConfigurationError` if not. There is no separate
    "required"/"owned" declaration. The field itself is the declaration,
    and two packages share an entry simply by their fields resolving to the
    same name.

    Attributes
    ----------
    tool_name : str
        Key used in ``[tools.<name>]``. Must be set on every subclass.
    extra_logging_namespaces : tuple[str, ...]
        Logger namespaces of transitive dependencies to configure alongside
        this package. The package's own ``tool_name``
        is always included; only list additional roots here, e.g.
        ``("my_extra_package_to_log",)``. Missing namespaces are harmless.
    """

    tool_name: ClassVar[str]
    extra_logging_namespaces: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def get_config(cls) -> Self:
        """Load this package's config from the active stack config file."""
        from .resolver import Resolver

        return Resolver.from_active_config().resolve_package_config(cls)

    @classmethod
    def configure_logging(cls, config=None, *, verbosity: int = 0, console=None) -> None:
        """Configure logging for this package and its declared transitive dependencies."""
        from .logging_config import configure_logging as _configure_logging

        _configure_logging(
            config,
            verbosity=verbosity,
            extra_namespaces=list(cls.extra_logging_namespaces) + [cls.tool_name],
            console=console,
        )

    @classmethod
    def get_engine(cls, database: str, **engine_kwargs: Any) -> Any:
        """Create a SQLAlchemy engine for a database.

        Parameters
        ----------
        database : str
            The database name to resolve, typically read off your own
            resolved config (e.g. ``MyPackageConfig.get_config().cdm_db``).
        **engine_kwargs:
            Forwarded to :meth:`~oa_configurator.resolver.ResolvedDatabase.create_engine`.

        Raises
        ------
        TypeError
            If *database* is a CDM entry. Use ``resolve_database(database).create_engines()``.
        """
        from .resolver import Resolver

        return Resolver.from_active_config().resolve_engine(database, **engine_kwargs)

    def to_extra_dict(self) -> dict[str, Any]:
        """Serialize back to the dict stored under ``[tools.<tool_name>]``."""
        return self.model_dump(exclude_none=True)

    def to_explicit_dict(self) -> dict[str, Any]:
        """Serialize only the fields that were supplied, not ones filled by defaults."""
        return self.model_dump(exclude_none=True, include=set(self.model_fields_set))

    @classmethod
    def unknown_keys(cls, config: StackConfig) -> tuple[str, ...]:
        """Return the stored ``[tools.<tool_name>]`` keys this class has no field for."""
        section = config.tools.get(cls.tool_name, {})
        return tuple(sorted(set(section) - set(cls.model_fields)))

    @classmethod
    def commit_section(cls, config: StackConfig, values: Mapping[str, Any]) -> Self:
        """Store *values* in *config* and validate, keeping unknown stored keys.

        Known fields are replaced by the normalized explicit values. Keys the
        class does not declare are carried over unchanged. *config* is
        modified in place.
        """
        section = config.tools.get(cls.tool_name, {})
        unknown = {key: section[key] for key in cls.unknown_keys(config)}
        config.tools[cls.tool_name] = {**unknown, **values}
        validated = cls.validate_candidate(config)
        config.tools[cls.tool_name] = {**unknown, **validated.to_explicit_dict()}
        return validated

    @classmethod
    def validate_candidate(cls, config: StackConfig) -> Self:
        """Validate this package's section and references in *config*.

        This is the package-aware apply boundary for the otherwise untyped
        ``StackConfig.tools`` mapping. Field and model-validator failures raise
        :class:`PackageConfigInvalidError`; reference failures raise
        :class:`ConfigurationError`. Neither path performs file I/O.
        """
        from .resolver import Resolver

        error: PackageConfigInvalidError | None = None
        try:
            return Resolver(config).resolve_package_config(cls)
        except ValidationError as exc:
            error = PackageConfigInvalidError(cls.tool_name, exc)
        assert error is not None
        raise error from None

    @classmethod
    def resolve_fields(
        cls,
        config: StackConfig,
        *,
        set_dict: dict[str, Any],
        interactive: bool,
    ) -> dict[str, Any]:
        """Resolve this package's own fields: flag (``--set`` or the field's
        own auto-generated flag), then stored, then an interactive prompt,
        recursing into any ``RefTo``-marked field via the generic resolver
        machinery in :mod:`~oa_configurator.resolver`.

        Only values that are flagged, already stored, or entered at a prompt
        are returned. At a prompt, Enter keeps the stored value and leaves an
        unset field unset; any typed value, including the default, is
        returned.

        A ``RefTo``-marked field's ``set_dict`` value may also be a nested
        ``dict`` instead of a plain string, built from repeated
        ``--set field.subfield=value`` CLI flags. Using a nested dict
        creates the target entry from those flags in the same call,
        instead of requiring it to already exist.

        Parameters
        ----------
        config : StackConfig
            The current StackConfig, used to read any already-stored extras.
        set_dict : dict[str, Any]
            Flag values, keyed by field name. Checked first. A value is
            either the field's plain string value, or (for a ``RefTo``
            field only) a nested ``dict`` of the target's own field values.
        interactive : bool
            Whether to prompt for fields not covered by set_dict, and
            whether an already-stored value is offered for change rather than
            reused silently. Non-interactively, fields covered by neither are
            omitted and take the field's own pydantic default when the config
            class is loaded.

        Returns
        -------
        dict[str, Any]
            Resolved extra field values, keyed by field name.

        Raises
        ------
        ConfigIssuesError
            If resolution fails; ``issues`` holds the structured problems.
            Nothing is printed.
        """
        import typer
        from rich.console import Console

        from .resolver import (
            _check_missing_required,
            _check_unrecognized_keys,
            _is_flag_settable,
            _nested_ref,
            _resolve_nested_flag_value,
            _resolve_ref,
        )

        console = Console()

        _check_unrecognized_keys(cls.tool_name, set_dict, cls.model_fields.keys())

        section = config.tools.get(cls.tool_name, {})

        extra: dict[str, Any] = {}
        missing_required: list[str] = []
        for field_name, info in cls.model_fields.items():
            nested = _nested_ref(info)
            stored = section.get(field_name)
            raw_set = set_dict.get(field_name)

            if isinstance(raw_set, dict) and nested is not None:
                is_test = nested.is_test
                resolved_name = _resolve_nested_flag_value(
                    field_name,
                    info,
                    nested,
                    raw_set,
                    config,
                    name_hint=field_name,
                    is_test=is_test,
                    missing_required=missing_required,
                )
                if resolved_name is not None:
                    extra[field_name] = resolved_name
            elif field_name in set_dict:
                extra[field_name] = set_dict[field_name]
            elif not interactive:
                if stored is not None:
                    extra[field_name] = stored
            elif nested is not None:
                is_test = nested.is_test
                if stored is None and info.default is None:
                    if is_test:
                        console.print("\n[dim]─── Test database (optional) ───[/dim]")
                        console.print(
                            "[yellow]⚠[/yellow]  Test databases are used by the test suite, which runs"
                            " DROP SCHEMA CASCADE on every run.\n"
                            "   Point to a [bold]dedicated test_only connection[/bold], never to real data."
                        )
                    else:
                        desc = info.description or ""
                        console.print(
                            f"\n[dim]─── {field_name} (optional){f': {desc}' if desc else ''} ───[/dim]"
                        )
                    if not typer.confirm(f"Configure {field_name}?", default=False):
                        continue
                default_name = (
                    stored
                    if stored is not None
                    else (
                        str(info.default)
                        if info.default not in (None, PydanticUndefined)
                        else field_name
                    )
                )
                resolved = _resolve_ref(
                    field_name,
                    info.description or "",
                    nested.target,
                    config,
                    default_name=default_name,
                    is_test=is_test,
                )
                if resolved:
                    extra[field_name] = resolved
            elif not _is_flag_settable(info):
                # dict/list fields have no sensible free-text prompt;
                # carry over an existing value, else leave to pydantic's
                # own default/default_factory.
                if stored is not None:
                    extra[field_name] = stored
            elif info.annotation is bool:
                answer = _prompt_optional_bool(
                    info.description or field_name, _enter_hint(stored, info)
                )
                if answer is not None:
                    extra[field_name] = answer
                elif stored is not None:
                    extra[field_name] = stored
            else:
                desc = info.description or ""
                label = f"{field_name}" + (f"  ({desc})" if desc else "")
                hint = _enter_hint(stored, info)
                raw = typer.prompt(label + hint, default="", show_default=False)
                if raw:
                    extra[field_name] = raw
                elif stored is not None:
                    extra[field_name] = stored

        _check_missing_required(
            missing_required, non_interactive=not interactive, package=cls.tool_name
        )
        return extra

    @classmethod
    def run_configure(cls, set_dict: dict[str, Any], *, interactive: bool) -> None:
        """Run the configure flow for this package: resolve every one of its
        own fields (see :meth:`resolve_fields`) and save to the active
        stack config file.
        """
        from rich.console import Console

        from .cli_support import _exit_on_config_issues, _save_stack_config_or_exit
        from .loader import active_config_path, load_stack_config
        from .stack_config import StackConfig

        console = Console()

        try:
            config = load_stack_config()
        except FileNotFoundError:
            active_config_path().parent.mkdir(parents=True, exist_ok=True)
            config = StackConfig()

        tool_name = cls.tool_name
        console.print(f"\n[bold]Configuring [cyan]{tool_name}[/cyan][/bold]")
        console.print(f"[dim]TOML section: \\[tools.{tool_name}][/dim]")

        with _exit_on_config_issues():
            extra = cls.resolve_fields(config, set_dict=set_dict, interactive=interactive)

        unknown = cls.unknown_keys(config)
        try:
            cls.commit_section(config, extra)
        except ConfigurationError as exc:
            import typer
            from rich.markup import escape

            Console(stderr=True).print(
                f"[red bold]Configuration rejected:[/red bold] {escape(str(exc))}"
            )
            raise typer.Exit(1) from exc

        _save_stack_config_or_exit(config)
        console.print(
            f"\n[green]✓[/green] Saved \\[tools.{tool_name}] to [dim]{active_config_path()}[/dim]"
        )
        if unknown:
            console.print(
                f"[yellow]Kept unrecognised keys:[/yellow] {', '.join(unknown)}. "
                "Run 'omop-config prune' to review them."
            )


@dataclass(frozen=True)
class PackagePlan:
    """A planned package section together with how its values were obtained.

    Attributes
    ----------
    config : StackConfig
        The complete validated candidate stack. Left out of the repr because
        tool sections may hold secrets.
    defaulted_fields : tuple[str, ...]
        Fields of the package that are not stored in the candidate and take
        their value from the class default.
    unknown_fields : tuple[str, ...]
        Stored keys the package has no field for. They are kept as they are.
        Only names are reported, never values.
    """

    config: StackConfig = field(repr=False)
    defaulted_fields: tuple[str, ...]
    unknown_fields: tuple[str, ...]


def plan_package_config(
    package_config: type[PackageConfigBase],
    config: StackConfig,
    set_dict: Mapping[str, object],
) -> PackagePlan:
    """Return a validated package candidate and how its values were obtained.

    Only values already stored, passed in *set_dict*, or reached through
    ``RefTo`` fields are written to the section. Defaults used for validation
    are reported in ``defaulted_fields`` and not stored. Stored keys the
    package does not declare are kept and reported in ``unknown_fields``.

    The input stack is deep-copied before field resolution, including nested
    ``RefTo`` creation or updates. The returned stack preserves the input's
    ``loaded_path`` as provenance and the caller's stack remains unchanged on
    success and failure. Nothing is printed.

    Raises
    ------
    ConfigIssuesError
        If field resolution fails; ``issues`` holds the structured problems.
    ConfigurationError
        If the assembled candidate fails package or reference validation.
    """
    from .stack_config import StackConfig

    candidate = config.model_copy(deep=True)
    unknown = package_config.unknown_keys(candidate)
    values = package_config.resolve_fields(candidate, set_dict=dict(set_dict), interactive=False)
    validated = package_config.commit_section(candidate, values)

    # Reconstruct the full root after nested resolution so all core section
    # references are checked too, without carrying any accidental shared state.
    planned = StackConfig.model_validate(candidate.model_dump(mode="python"))
    if config.loaded_path is not None:
        planned.bind_loaded_path(config.loaded_path)
    defaulted = tuple(
        name for name in package_config.model_fields if name not in validated.model_fields_set
    )
    return PackagePlan(planned, defaulted, unknown)


def plan_configure(
    package_config: type[PackageConfigBase],
    config: StackConfig,
    set_dict: Mapping[str, object],
) -> StackConfig:
    """Return the candidate stack of :func:`plan_package_config`."""
    return plan_package_config(package_config, config, set_dict).config


@dataclass(frozen=True)
class PruneReport:
    """The result of :func:`prune_unknown_keys`.

    Attributes
    ----------
    config : StackConfig
        A validated copy of the stack with the unknown keys removed. Left out
        of the repr because tool sections may hold secrets.
    removed : dict[str, tuple[str, ...]]
        Removed key names per tool name. Values are never reported.
    """

    config: StackConfig = field(repr=False)
    removed: dict[str, tuple[str, ...]]


def prune_unknown_keys(
    config: StackConfig, package_configs: Iterable[type[PackageConfigBase]]
) -> PruneReport:
    """Remove stored keys that the given package classes do not declare.

    Only ``[tools.*]`` sections that have a class in *package_configs* are
    touched, and only keys with no matching field. The input is left
    unchanged and nothing is written to disk. A key unknown to this version
    may belong to a newer one that shares the file.
    """
    from .stack_config import StackConfig

    pruned = config.model_copy(deep=True)
    removed: dict[str, tuple[str, ...]] = {}
    for package_config in package_configs:
        unknown = package_config.unknown_keys(pruned)
        if not unknown:
            continue
        section = pruned.tools[package_config.tool_name]
        pruned.tools[package_config.tool_name] = {
            key: value for key, value in section.items() if key not in unknown
        }
        removed[package_config.tool_name] = unknown
    result = StackConfig.model_validate(pruned.model_dump(mode="python"))
    if config.loaded_path is not None:
        result.bind_loaded_path(config.loaded_path)
    return PruneReport(result, removed)
