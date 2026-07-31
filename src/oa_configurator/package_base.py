"""Base class for per-package configuration sections.

Each package that wants to support ``omop-config configure <name>`` subclasses
:class:`PackageConfigBase`, declares its typed fields, and registers itself via
the ``omop.config`` entry-point group in its ``pyproject.toml``.

Example
-------
In ``<my-package>/config.py``::

    from typing import Annotated, ClassVar

    from oa_configurator import DatabaseConfig, ModelConfig, PackageConfigBase, RefTo

    class MyPackageConfig(PackageConfigBase):
        tool_name: ClassVar[str] = "<my-package>"
        # A field naming an entry in another section. `omop-config configure`
        # offers to reuse an existing one or create it on the spot, recursing
        # into any RefTo fields the target itself has (e.g. a database's own
        # connection). No separate declaration list: the field IS the
        # declaration. Whether the entry ends up shared with another package
        # is simply a matter of both packages' fields resolving to the same
        # name.
        cdm_db: Annotated[str, RefTo(DatabaseConfig)] = "cdm_db"
        embedding_model_name: Annotated[str, RefTo(ModelConfig)] = "embed-default"
        <additional typed fields here>

    config = MyPackageConfig.get_config()

In ``pyproject.toml``::

    [project.entry-points."omop.config"]
    <my-package> = "<my-package>.config:<MyPackageConfig>"
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

if TYPE_CHECKING:
    from .stack_config import StackConfig


class ConfigurationError(ValueError):
    """Raised when a required database or connection is missing from the stack config."""


class PackageConfigBase(BaseModel):
    """Typed view over a package's ``[tools.<tool_name>]`` TOML section.

    Subclass this and declare typed fields for whatever this package needs.
    A field typed ``Annotated[str, RefTo(DatabaseConfig)]`` (or ``RefTo(ModelConfig)``/
    ``RefTo(ProviderConfig)``/``RefTo(ConnectionConfig)``) names an entry in that
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
        """
        from .resolver import Resolver
        return Resolver.from_active_config().resolve_engine(database, **engine_kwargs)

    def to_extra_dict(self) -> dict[str, Any]:
        """Serialize back to the dict stored under ``[tools.<tool_name>]``."""
        return self.model_dump(exclude_none=True)

    @classmethod
    def resolve_fields(cls, config: StackConfig, *, set_dict: dict[str, Any], interactive: bool) -> dict[str, Any]:
        """Resolve this package's own fields: flag (``--set`` or the field's
        own auto-generated flag), then stored, then an interactive prompt
        (seeded with the stored value as its default when one exists),
        recursing into any ``RefTo``-marked field via the generic resolver
        machinery in :mod:`~oa_configurator.resolver`.

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
            Whether to prompt for fields not covered by set_dict or stored
            config, and whether an already-stored value is offered as a
            re-promptable default rather than reused silently.
            Non-interactively, fields covered by neither are simply omitted
            (they fall back to the field's own pydantic default when the
            config class is loaded).

        Returns
        -------
        dict[str, Any]
            Resolved extra field values, keyed by field name.

        Raises
        ------
        typer.Exit
            If a nested ``--set`` creation (see above) is missing a
            required field of the target it's creating, non-interactively.
        """
        import typer
        from rich.console import Console

        from .resolver import Resolver, _check_missing_required, _nested_ref, _resolve_nested_flag_value, _resolve_ref

        console = Console()

        try:
            current = Resolver(config).resolve_package_config(cls)
            current_dict = current.to_extra_dict()
        except (ConfigurationError, ValueError):
            current_dict = {}

        extra: dict[str, Any] = {}
        missing_required: list[str] = []
        for field_name, info in cls.model_fields.items():
            nested = _nested_ref(info)
            stored = current_dict.get(field_name)
            raw_set = set_dict.get(field_name)

            if isinstance(raw_set, dict) and nested is not None:
                is_test = field_name.startswith("test_")
                resolved_name = _resolve_nested_flag_value(
                    field_name, info, nested, raw_set, config,
                    name_hint=field_name, is_test=is_test, missing_required=missing_required,
                )
                if resolved_name is not None:
                    extra[field_name] = resolved_name
            elif field_name in set_dict:
                extra[field_name] = set_dict[field_name]
            elif not interactive:
                if stored is not None:
                    extra[field_name] = stored
            elif nested is not None:
                is_test = field_name.startswith("test_")
                if is_test and stored is None and info.default is None:
                    console.print("\n[dim]─── Test database (optional) ───[/dim]")
                    console.print(
                        "[yellow]⚠[/yellow]  Test databases are used by the test suite, which runs"
                        " DROP SCHEMA CASCADE on every run.\n"
                        "   Point to a [bold]dedicated test_only connection[/bold], never to real data."
                    )
                    if not typer.confirm(f"Configure {field_name}?", default=False):
                        continue
                default_name = stored if stored is not None else (
                    str(info.default) if info.default not in (None, PydanticUndefined) else field_name
                )
                resolved = _resolve_ref(
                    field_name, info.description or "", nested.target, config,
                    default_name=default_name, is_test=is_test,
                )
                if resolved:
                    extra[field_name] = resolved
            else:
                desc = info.description or ""
                label = f"{field_name}" + (f"  ({desc})" if desc else "")
                default_value = stored if stored is not None else (str(info.default) if info.default is not None else "")
                raw = typer.prompt(label, default=default_value)
                if raw and raw != "None":
                    extra[field_name] = raw

        _check_missing_required(f"tool {cls.tool_name!r}", missing_required, non_interactive=not interactive)
        return extra

    @classmethod
    def run_configure(cls, set_dict: dict[str, Any], *, interactive: bool) -> None:
        """Run the configure flow for this package: resolve every one of its
        own fields (see :meth:`resolve_fields`) and save to the active
        stack config file.
        """
        from rich.console import Console

        from .io import save_stack_config
        from .loader import CONFIG_PATH, load_stack_config
        from .stack_config import StackConfig

        console = Console()

        try:
            config = load_stack_config()
        except FileNotFoundError:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            config = StackConfig()

        tool_name = cls.tool_name
        console.print(f"\n[bold]Configuring [cyan]{tool_name}[/cyan][/bold]")
        console.print(f"[dim]TOML section: \\[tools.{tool_name}][/dim]")

        extra = cls.resolve_fields(config, set_dict=set_dict, interactive=interactive)

        config.tools[tool_name] = extra
        save_stack_config(config)
        console.print(f"\n[green]✓[/green] Saved \\[tools.{tool_name}] to [dim]{CONFIG_PATH}[/dim]")
