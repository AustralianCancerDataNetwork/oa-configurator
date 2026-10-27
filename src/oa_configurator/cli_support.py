"""Shared CLI plumbing used by both the root aggregator (cli.py) and every
domain's own CLI module (domains/*/cli.py).

Lives outside cli.py itself: cli.py imports each domain's Typer sub-app to
mount it (app.add_typer(connections_app, ...)), so if these helpers
lived in cli.py, every domain module importing them back would be
circular. typer/rich/click are already unconditional oa-configurator
dependencies; imported lazily inside each function body regardless, so a
consumer that never touches the CLI doesn't pay to load them.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any, NoReturn

from pydantic import BaseModel

from .package_base import ConfigurationError
from .references import (
    Sectioned,
    _require_new_name,
    clone_entry,
    delete_entry,
    discover_package_configs,
    mismatched_kind_refs,
    remove_tool_section,
    unresolved_refs,
)
from .resolver import _check_missing_required, _flag_name, _is_flag_settable, _resolve_named_entry
from .sensitive import MASK, is_sensitive
from .stack_config import StackConfig


def _save_stack_config_or_exit(
    config: StackConfig,
    *,
    save: Callable[[StackConfig], object] | None = None,
) -> None:
    """Save for a CLI flow, rendering filesystem failures without a traceback."""
    import typer
    from rich.console import Console
    from rich.markup import escape

    if save is None:
        from .io import save_stack_config

        save = save_stack_config

    try:
        save(config)
    except OSError as exc:
        Console(stderr=True).print(
            f"[red bold]Could not save configuration:[/red bold] {escape(str(exc))}"
        )
        raise typer.Exit(1) from None


def _build_entry_params(target: type[BaseModel]) -> list[inspect.Parameter]:
    """Build one optional ``--flag`` signature parameter per field of *target*."""
    import typer

    return [
        inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=Annotated[
                str | None,
                typer.Option(_flag_name(name), help=info.description or ""),
            ],
        )
        for name, info in target.model_fields.items()
    ]


def _check_entry_refs(entry: BaseModel, config: StackConfig) -> None:
    """Verify every RefTo-marked field on *entry* resolves, aborting with a
    clear error immediately. Otherwise a dangling reference would save
    silently and only surface as a load-time error the next time anyone
    reads the config file.
    """
    import typer
    from rich.console import Console

    err_console = Console(stderr=True)
    for _field_name, value, section in unresolved_refs(entry, config):
        err_console.print(
            f"[red bold]Unknown {section[:-1]} {value!r}.[/red bold] Configure it first: "
            f"omop-config {section} add {value}"
        )
        raise typer.Exit(1)
    for _field_name, value, expected, actual in mismatched_kind_refs(entry, config):
        err_console.print(
            f"[red bold]{value!r} is a {actual.__name__}, not a {expected.__name__}.[/red bold] "
            f"Point it at a matching entry instead."
        )
        raise typer.Exit(1)


# ------------------
# Shared body for every `<section> add/list <name>` command, used identically
# by all four leaf sections (connections/databases in domains/resources,
# providers/models in domains/llm).
# ------------------


def _add_entry(target: type[Sectioned], name: str, flags: dict[str, Any] | None) -> None:
    """Shared body for every `<section> add <name>` command.

    Before saving, re-validates the whole config with the new entry
    substituted in, so replacing an existing entry with one of a
    different concrete class (e.g. re-adding a database under a
    different ``--kind``) is refused if something still depends on the
    old class, instead of silently breaking that reference.
    """
    import typer
    from rich.console import Console

    from .loader import active_config_path, load_stack_config

    console = Console()
    err_console = Console(stderr=True)
    section = target.section

    try:
        config = load_stack_config()
    except FileNotFoundError:
        active_config_path().parent.mkdir(parents=True, exist_ok=True)
        config = StackConfig()

    section_dict = getattr(config, section)
    existing = section_dict.get(name)
    missing_required: list[str] = []
    entry = _resolve_named_entry(
        target, config, flags=flags, existing=existing, missing_required=missing_required
    )
    _check_missing_required(
        f"{section[:-1]} {name!r}", missing_required, non_interactive=flags is not None
    )
    assert entry is not None  # guaranteed by the missing_required check above
    _check_entry_refs(entry, config)

    if existing is not None and type(existing) is not type(entry):
        console.print(
            f"[yellow]⚠[/yellow]  {name!r} was a {type(existing).__name__}, now a "
            f"{type(entry).__name__}. Anything referencing it as the old type will break."
        )

    candidate_sections = {s: dict(getattr(config, s)) for s in StackConfig.entry_sections()}
    candidate_sections[section][name] = entry
    try:
        StackConfig(**candidate_sections, tools=config.tools, logging=config.logging)
    except ValueError as exc:
        err_console.print(
            f"[red bold]Saving {name!r} would break an existing reference:[/red bold] {exc}"
        )
        raise typer.Exit(1)

    section_dict[name] = entry
    _save_stack_config_or_exit(config)
    console.print(
        f"[green]✓[/green] Saved \\[{section}.{name}] to [dim]{active_config_path()}[/dim]"
    )


def _list_entries(target: type[Sectioned]) -> None:
    """Shared body for every `<section> list` command."""
    import typer
    from rich.console import Console
    from rich.table import Table

    from .loader import load_stack_config

    # explicit width to prevent truncation
    console = Console(width=200)
    err_console = Console(stderr=True)
    section = target.section

    try:
        config = load_stack_config()
    except FileNotFoundError:
        from .loader import active_config_path

        err_console.print(f"[red]Config file not found:[/red] {active_config_path()}")
        raise typer.Exit(1)

    section_dict: dict[str, Any] = getattr(config, section)
    if not section_dict:
        console.print(f"[yellow]No {section} configured.[/yellow]")
        return

    # Union of fields across every entry's own runtime type, not just
    # *target*'s, so a subclass-only column (e.g. vocab_connection) isn't hidden.
    field_infos: dict[str, Any] = dict(target.model_fields)
    for entry in section_dict.values():
        for n, info in type(entry).model_fields.items():
            field_infos.setdefault(n, info)
    field_names = [n for n, info in field_infos.items() if _is_flag_settable(info)]

    table = Table("Name", *field_names)
    for entry_name in sorted(section_dict):
        entry = section_dict[entry_name]
        row = [_cell(getattr(entry, f, None), field_infos[f]) for f in field_names]
        table.add_row(entry_name, *row)
    console.print(table)


def _cell(value: Any, info: Any) -> str:
    """Render one field for a `<section> list` table."""
    if value in (None, ""):
        return "[dim]-[/dim]"
    return MASK if is_sensitive(info) else str(value)


def _fail(title: str, detail: str) -> NoReturn:
    """Print a red error line to stderr and exit with status 1."""
    import typer
    from rich.console import Console
    from rich.markup import escape

    Console(stderr=True).print(f"[red bold]{title}[/red bold] {escape(detail)}")
    raise typer.Exit(1)


def _load_existing_or_exit() -> StackConfig:
    """Load the active config, or exit if the file is missing."""
    import typer
    from rich.console import Console

    from .loader import active_config_path, load_stack_config

    try:
        return load_stack_config()
    except FileNotFoundError:
        err_console = Console(stderr=True)
        err_console.print(f"[red]Config file not found:[/red] {active_config_path()}")
        err_console.print("Run [bold]omop-config init[/bold] to create it.")
        raise typer.Exit(1) from None


def _print_package_problems(
    config: StackConfig, package_configs: dict[str, Any], problems: tuple[Any, ...]
) -> None:
    """Explain tool sections whose package is unusable, if the config has any."""
    from rich.console import Console
    from rich.markup import escape

    unknown = [name for name in config.tools if name not in package_configs]
    if not unknown:
        return
    err_console = Console(stderr=True)
    err_console.print(
        "\n[yellow]These tool sections have no usable package, so references "
        "from them cannot be checked:[/yellow]"
    )
    for name in unknown:
        err_console.print(f"  \\[tools.{name}]")
    for problem in problems:
        err_console.print(
            f"  {escape(problem.name)}: {problem.kind.value} ({escape(problem.detail)})"
        )
    err_console.print(
        "Fix the install, or remove a stale section with [bold]omop-config tools remove NAME[/bold]."
    )


def _clone_entry(
    target: type[Sectioned], source_name: str, new_name: str, flags: dict[str, Any] | None
) -> None:
    """Shared body for every `<section> clone <source> <new>` command.

    Resolves the copy the way ``add`` does, with the source entry as the stored
    defaults, then lets :func:`~oa_configurator.references.clone_entry` build
    and validate the candidate from the fields that changed.
    """
    from rich.console import Console

    from .loader import active_config_path

    console = Console()
    section = target.section
    singular = section[:-1]

    config = _load_existing_or_exit()
    entries = getattr(config, section)
    source = entries.get(source_name)
    if source is None:
        _fail("Could not clone.", f"No {singular} named {source_name!r}.")
    if not isinstance(source, target):
        _fail(
            "Could not clone.",
            f"{section}.{source_name} is a {type(source).__name__}, not a {target.__name__}. "
            "Use the clone command for its kind.",
        )
    try:
        _require_new_name(entries, section, new_name)
    except ConfigurationError as exc:
        _fail("Could not clone.", str(exc))

    missing_required: list[str] = []
    entry = _resolve_named_entry(
        target, config, flags=flags, existing=source, missing_required=missing_required
    )
    _check_missing_required(
        f"{singular} {new_name!r}", missing_required, non_interactive=flags is not None
    )
    assert entry is not None  # guaranteed by the missing_required check above
    _check_entry_refs(entry, config)

    changed = {
        name: getattr(entry, name)
        for name in target.model_fields
        if name != "kind" and getattr(entry, name) != getattr(source, name)
    }
    try:
        planned = clone_entry(config, section, source_name, new_name, overrides=changed)
    except ConfigurationError as exc:
        _fail(f"Cloning {source_name!r} to {new_name!r} would be invalid.", str(exc))

    _save_stack_config_or_exit(planned)
    console.print(
        f"[green]✓[/green] Saved \\[{section}.{new_name}] to [dim]{active_config_path()}[/dim]"
    )


def _delete_entry(target: type[Sectioned], name: str, yes: bool) -> None:
    """Shared body for every `<section> delete <name>` command."""
    import typer
    from rich.console import Console

    from .loader import active_config_path

    section = target.section
    config = _load_existing_or_exit()
    package_configs, problems = discover_package_configs()
    try:
        planned = delete_entry(config, section, name, package_configs=package_configs)
    except ConfigurationError as exc:
        from rich.markup import escape

        Console(stderr=True).print(f"[red bold]Could not delete.[/red bold] {escape(str(exc))}")
        _print_package_problems(config, package_configs, problems)
        raise typer.Exit(1) from None

    if not yes:
        typer.confirm(f"Delete {section[:-1]} {name!r}?", abort=True)
    _save_stack_config_or_exit(planned)
    Console().print(
        f"[green]✓[/green] Deleted \\[{section}.{name}] from [dim]{active_config_path()}[/dim]"
    )


def _list_tools() -> None:
    """Shared body for `tools list`: each tool section and whether its package is usable."""
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    console = Console()
    config = _load_existing_or_exit()
    if not config.tools:
        console.print("[yellow]No tool sections configured.[/yellow]")
        return
    package_configs, problems = discover_package_configs()
    table = Table("Tool", "Package")
    for name in sorted(config.tools):
        table.add_row(
            name, "installed" if name in package_configs else "[yellow]not available[/yellow]"
        )
    console.print(table)
    for problem in problems:
        console.print(
            f"[dim]{escape(problem.name)}: {problem.kind.value} ({escape(problem.detail)})[/dim]"
        )


def _remove_tool(name: str, yes: bool) -> None:
    """Shared body for `tools remove <name>`."""
    import typer
    from rich.console import Console

    from .loader import active_config_path

    config = _load_existing_or_exit()
    try:
        planned = remove_tool_section(config, name)
    except ConfigurationError as exc:
        _fail("Could not remove.", str(exc))

    if not yes:
        typer.confirm(
            f"Remove [tools.{name}]? Its settings are deleted from the config; "
            "the previous file is kept as a .bak copy.",
            abort=True,
        )
    _save_stack_config_or_exit(planned)
    Console().print(
        f"[green]✓[/green] Removed \\[tools.{name}] from [dim]{active_config_path()}[/dim]"
    )


def _register_command(
    app: Any,
    name: str,
    *,
    help: str,
    params: list[inspect.Parameter],
    run: Callable[..., None],
) -> None:
    """Register *run* on *app* as a command whose Typer signature is *params*."""

    def callback(**kwargs: Any) -> None:
        run(**kwargs)

    callback.__signature__ = inspect.Signature(params)  # ty: ignore[unresolved-attribute]
    callback.__annotations__ = {p.name: p.annotation for p in params}
    app.command(name, help=help)(callback)


def _register_clone(app: Any, target: type[Sectioned], *, name: str = "clone", help: str) -> None:
    """Add a ``clone <source> <new>`` command to *app* with one flag per settable field."""
    import typer

    singular = target.section[:-1]
    arguments = [
        inspect.Parameter(
            "source_name",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Annotated[
                str, typer.Argument(metavar="SOURCE", help=f"Existing {singular} to copy.")
            ],
        ),
        inspect.Parameter(
            "new_name",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Annotated[
                str, typer.Argument(metavar="NEW", help=f"Name of the new {singular}.")
            ],
        ),
    ]
    flags = [
        param
        for param in _build_entry_params(target)
        if _is_flag_settable(target.model_fields[param.name])
    ]
    flag_names = {param.name for param in flags}

    def run(**kwargs: Any) -> None:
        values = {k: v for k, v in kwargs.items() if k in flag_names and v is not None}
        _clone_entry(target, kwargs["source_name"], kwargs["new_name"], values or None)

    _register_command(app, name, help=help, params=[*arguments, *flags], run=run)


def _register_delete(app: Any, target: type[Sectioned], *, help: str) -> None:
    """Add a ``delete <name>`` command to *app* that confirms before deleting."""
    import typer

    singular = target.section[:-1]
    params = [
        inspect.Parameter(
            "name",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Annotated[
                str, typer.Argument(metavar="NAME", help=f"Name of the {singular} to delete.")
            ],
        ),
        inspect.Parameter(
            "yes",
            inspect.Parameter.KEYWORD_ONLY,
            default=False,
            annotation=Annotated[
                bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
            ],
        ),
    ]

    def run(**kwargs: Any) -> None:
        _delete_entry(target, kwargs["name"], kwargs["yes"])

    _register_command(app, "delete", help=help, params=params, run=run)
