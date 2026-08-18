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

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from .refs import MASK, is_sensitive
from .stack_config import StackConfig, mismatched_kind_refs, unresolved_refs
from .resolver import _check_missing_required, _flag_name, _is_flag_settable, _resolve_named_entry


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


def _build_entry_params(target: type[BaseModel]) -> list[Any]:
    """Generate Click options for one target schema's own fields."""
    import click

    return [
        click.Option(
            [_flag_name(name)],
            default=None,
            type=click.STRING,
            help=info.description or "",
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

def _add_entry(target: type[BaseModel], section: str, name: str, flags: dict[str, Any] | None) -> None:
    """Shared body for every `<section> add <name>` command.

    Before saving, re-validates the whole config with the new entry
    substituted in, so replacing an existing entry with one of a
    different concrete class (e.g. re-adding a database under a
    different ``--kind``) is refused if something still depends on the
    old class, instead of silently breaking that reference.
    """
    import typer
    from rich.console import Console

    from .loader import CONFIG_PATH, load_stack_config

    console = Console()
    err_console = Console(stderr=True)

    try:
        config = load_stack_config()
    except FileNotFoundError:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config = StackConfig()

    section_dict = getattr(config, section)
    existing = section_dict.get(name)
    missing_required: list[str] = []
    entry = _resolve_named_entry(target, config, flags=flags, existing=existing, missing_required=missing_required)
    _check_missing_required(f"{section[:-1]} {name!r}", missing_required, non_interactive=flags is not None)
    assert entry is not None  # guaranteed by the missing_required check above
    _check_entry_refs(entry, config)

    if existing is not None and type(existing) is not type(entry):
        console.print(
            f"[yellow]⚠[/yellow]  {name!r} was a {type(existing).__name__}, now a "
            f"{type(entry).__name__}. Anything referencing it as the old type will break."
        )

    candidate_sections = {
        s: dict(getattr(config, s)) for s in ("connections", "databases", "providers", "models", "vector_stores")
    }
    candidate_sections[section][name] = entry
    try:
        StackConfig(**candidate_sections, tools=config.tools, logging=config.logging)
    except ValueError as exc:
        err_console.print(f"[red bold]Saving {name!r} would break an existing reference:[/red bold] {exc}")
        raise typer.Exit(1)

    section_dict[name] = entry
    _save_stack_config_or_exit(config)
    console.print(f"[green]✓[/green] Saved \\[{section}.{name}] to [dim]{CONFIG_PATH}[/dim]")


def _list_entries(target: type[BaseModel], section: str) -> None:
    """Shared body for every `<section> list` command."""
    import typer
    from rich.console import Console
    from rich.table import Table

    from .loader import load_stack_config

    console = Console()
    err_console = Console(stderr=True)

    try:
        config = load_stack_config()
    except FileNotFoundError:
        from .loader import CONFIG_PATH
        err_console.print(f"[red]Config file not found:[/red] {CONFIG_PATH}")
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
