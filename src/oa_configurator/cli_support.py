"""Shared CLI plumbing used by both the root aggregator (cli.py) and every
domain's own CLI module (domains/*/cli.py).

Lives outside cli.py itself: cli.py imports each domain's Typer sub-app to
mount it (``app.add_typer(connections_app, ...)``), so if these helpers
lived in cli.py, every domain module importing them back would be
circular. typer/rich/click are already unconditional oa-configurator
dependencies; imported lazily inside each function body regardless, so a
consumer that never touches the CLI doesn't pay to load them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .stack_config import StackConfig, unresolved_refs
from .resolver import _is_promptable, _resolve_named_entry


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


def _check_missing_required(
    display_name: str,
    missing_required: list[str],
    *,
    non_interactive: bool,
) -> None:
    """Abort with a clear error, naming exact CLI flags, if fields are missing."""
    if not non_interactive or not missing_required:
        return
    import typer
    from rich.console import Console

    err_console = Console(stderr=True)
    flag_names = ", ".join(_flag_name(k) for k in missing_required)
    err_console.print(
        f"\n[red bold]Missing required field(s) for {display_name!r}:[/red bold] {flag_names}\n"
        f"No flag or stored config is available for these. Pass them explicitly."
    )
    raise typer.Exit(1)


def _check_entry_refs(entry: BaseModel, config: StackConfig) -> None:
    """Verify every RefTo-marked field on *entry* resolves, aborting with a
    clear error immediately -- rather than silently saving a dangling
    reference that would only surface as a load-time error the next time
    anyone reads the config file.
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


# ------------------
# Shared body for every `<section> add/list <name>` command, used identically
# by all four leaf sections (connections/databases in domains/resources,
# providers/models in domains/llm).
# ------------------

def _add_entry(target: type[BaseModel], section: str, name: str, flags: dict[str, str] | None) -> None:
    """Shared body for every `<section> add <name>` command."""
    from rich.console import Console

    from .io import save_stack_config
    from .loader import CONFIG_PATH, load_stack_config

    console = Console()

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
    section_dict[name] = entry
    save_stack_config(config)
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

    field_names = [n for n, info in target.model_fields.items() if _is_promptable(info)]
    table = Table("Name", *field_names)
    for entry_name in sorted(section_dict):
        entry = section_dict[entry_name]
        row = ["[dim]-[/dim]" if (v := getattr(entry, f)) in (None, "") else str(v) for f in field_names]
        table.add_row(entry_name, *row)
    console.print(table)
