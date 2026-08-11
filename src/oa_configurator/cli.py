"""CLI for omop-config: initialise, inspect, test connections, configure packages.

Pure aggregator: discovers/dispatches registered packages' `configure`
subcommands and mounts each domain's own `<section> add/list` sub-app
(domains/resources/cli.py, domains/llm/cli.py). Per-package field
resolution/save lives on PackageConfigBase (resolve_fields/run_configure);
the generic recursive resolution engine lives in resolver.py; shared
`<section> add/list` plumbing lives in cli_support.py. This module owns
only discovery/dispatch and the handful of top-level commands
(init/show/verify/export-env).
"""

from __future__ import annotations

import time
from importlib.metadata import entry_points
from typing import Annotated, Any

import click
import rich
import sqlalchemy as sa
import typer
from typer.core import TyperGroup
from rich.console import Console
from rich.table import Table

from .cli_support import _build_entry_params
from .domains.llm.cli import models_app, providers_app
from .domains.resources.cli import connections_app, databases_app
from .domains.vector_stores.cli import vector_stores_app
from .io import save_stack_config, write_env_file
from .loader import CONFIG_PATH, load_stack_config
from .logging_config import configure_logging
from .stack_config import StackConfig
from .package_base import PackageConfigBase
from .resolver import Resolver

app = typer.Typer(name="omop-config", no_args_is_help=True, add_completion=False)
console = Console()
err_console = Console(stderr=True)

ENTRY_POINT_GROUP = "omop.config"

app.add_typer(connections_app, name="connections")
app.add_typer(databases_app, name="databases")
app.add_typer(providers_app, name="providers")
app.add_typer(models_app, name="models")
app.add_typer(vector_stores_app, name="vector-stores")


# ------------------
# Dynamic configure command that discovers packages via entry points and generates a subcommand for each.
# Per-package field resolution/save itself lives on PackageConfigBase
# (resolve_fields/run_configure). This part just discovers and dispatches.
# ------------------

class _DynamicConfigureGroup(TyperGroup):
    def __init__(self, **kwargs):
        kwargs.setdefault("invoke_without_command", True)
        super().__init__(**kwargs)

    def list_commands(self, ctx):
        return sorted(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP))

    def get_command(self, ctx, cmd_name):
        eps = {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}
        ep = eps.get(cmd_name)
        return _build_package_command(cmd_name, ep.load()) if ep else None


def _parse_set_flags(raw: tuple[str, ...]) -> dict[str, Any]:
    """Parse repeated ``--set path.to.field=value`` strings into a nested dict.

    A dotted path builds nested dicts, so ``--set cdm_db.dialect=sqlite
    --set cdm_db.host=db`` becomes ``{"cdm_db": {"dialect": "sqlite", "host": "db"}}``.
    Lets a non-interactive ``configure`` call create a brand-new RefTo
    target (e.g. a database and the connection it points at) in the same
    call that points a package's field at it, instead of requiring the
    target to already exist.
    """
    tree: dict[str, Any] = {}
    for item in raw:
        path, sep, value = item.partition("=")
        if not sep or not path:
            raise typer.BadParameter(f"--set value must be path=value, got {item!r}")
        *parents, leaf = path.split(".")
        node = tree
        for part in parents:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise typer.BadParameter(f"--set path conflict at {part!r} in {path!r}")
        node[leaf] = value
    return tree


def _build_package_command(ep_name: str, cls: type[PackageConfigBase]) -> click.Command:
    """Build a Click command for one registered package entry point."""
    extra_params = _build_entry_params(cls)
    extra_names = {p.name for p in extra_params}
    set_param = click.Option(
        ["--set", "set_values"],
        multiple=True,
        default=(),
        help=(
            "Set a nested field non-interactively, e.g. --set cdm_db.dialect=sqlite "
            "(repeatable). Lets a RefTo field's target be created in this same call "
            "instead of pointing at an already-existing entry."
        ),
    )

    def callback(**kwargs):
        set_values = kwargs.pop("set_values", ())
        set_dict: dict[str, Any] = {k: str(v) for k, v in kwargs.items() if k in extra_names and v is not None}
        parsed = _parse_set_flags(tuple(set_values))
        if clash := set(set_dict) & set(parsed):
            raise typer.BadParameter(
                f"--set targets {sorted(clash)}, also given as a flag. Use one or the other."
            )
        set_dict.update(parsed)
        cls.run_configure(set_dict, interactive=not set_dict)

    return click.Command(
        name=ep_name,
        callback=callback,
        params=[*extra_params, set_param],
        help=f"Configure {cls.tool_name} settings in config.toml.",
    )


def _list_packages() -> None:
    eps = entry_points(group=ENTRY_POINT_GROUP)
    registered = {ep.name: ep for ep in eps}
    if not registered:
        console.print("[yellow]No packages registered under 'omop.config' entry points.[/yellow]")
        console.print(
            "\nPackages add support in their pyproject.toml:\n"
            f'  [project.entry-points."{ENTRY_POINT_GROUP}"]\n'
            '  my-package = "my-package.config:MyPackageConfig"'
        )
    else:
        console.print("[bold]Registered packages:[/bold]")
        for name in sorted(registered):
            console.print(f"  • {name}")


@app.callback()  # required by Typer to attach global --verbose/-v before any subcommand
def _main(
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose", "-v",
            count=True,
            help="Increase log verbosity (-v INFO, -vv DEBUG). Must come before the subcommand name.",
        ),
    ] = 0,
) -> None:
    configure_logging(verbosity=verbose)


@app.command()
def init(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite existing config without prompting."),
    ] = False,
) -> None:
    """Create the config file at CONFIG_PATH (default ~/.config/omop/config.toml). Set OA_CONFIG_PATH to write elsewhere. Use 'omop-config configure <pkg>' to populate it."""
    if CONFIG_PATH.exists() and not force:
        overwrite = typer.confirm(
            f"Config already exists at {CONFIG_PATH}. Overwrite?",
            default=False,
        )
        if not overwrite:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(0)

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    save_stack_config(StackConfig())
    console.print(f"[green]✓[/green] Created [dim]{CONFIG_PATH}[/dim]")

    eps = entry_points(group=ENTRY_POINT_GROUP)
    if eps:
        console.print("\nRun configure for each installed package:")
        for ep in sorted(eps, key=lambda e: e.name):
            console.print(f"  omop-config configure {ep.name}")
    else:
        console.print("\nNo packages registered yet. Install a package that supports oa_configurator.")

@app.command()
def show() -> None:
    """Print the resolved configuration as JSON."""
    try:
        config = load_stack_config()
    except FileNotFoundError:
        err_console.print(f"[red]Config file not found:[/red] {CONFIG_PATH}")
        err_console.print("Run [bold]omop-config init[/bold] to create it.")
        raise typer.Exit(1)
    rich.print_json(config.model_dump_json(exclude_none=True, indent=2))


@app.command()
def verify() -> None:
    """Test all configured connections and report status."""
    try:
        config = load_stack_config()
    except FileNotFoundError:
        err_console.print(f"[red]Config file not found:[/red] {CONFIG_PATH}")
        raise typer.Exit(1)

    if not config.connections:
        console.print("[yellow]No connections configured.[/yellow]")
        return

    resolver = Resolver(config)
    table = Table("Connection", "URL", "Status", "Latency")
    all_ok = True

    for name in sorted(config.connections):
        try:
            target = resolver.resolve_connection(name)
        except Exception as exc:
            table.add_row(name, "?", "[red]FAIL[/red]", str(exc)[:60])
            all_ok = False
            continue
        try:
            t0 = time.monotonic()
            engine = target.create_engine()
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            elapsed = (time.monotonic() - t0) * 1000
            table.add_row(name, target.safe_url, "[green]OK[/green]", f"{elapsed:.0f} ms")
        except Exception as exc:
            table.add_row(name, target.safe_url, "[red]FAIL[/red]", str(exc)[:60])
            all_ok = False

    console.print(table)
    if not all_ok:
        raise typer.Exit(1)


@app.command("export-env")
def export_env() -> None:
    """Write CONFIG_PATH's sibling .env file (default ~/.config/omop/config.env) for Docker Compose env_file:."""
    try:
        config = load_stack_config()
    except FileNotFoundError:
        err_console.print(f"[red]Config file not found:[/red] {CONFIG_PATH}")
        raise typer.Exit(1)

    env_path = write_env_file(Resolver(config))
    console.print(f"[green]✓[/green] Wrote [dim]{env_path}[/dim]")


@app.command(name="configure", cls=_DynamicConfigureGroup)  # ty: ignore[invalid-argument-type]
def configure(ctx: typer.Context) -> None:
    r"""Configure a package's \[tools.<name>] section.

    Run 'omop-config configure <package> --help' to see that package's flags.
    Packages register support via the 'omop.config' entry-point group.
    """
    if ctx.invoked_subcommand is None:
        _list_packages()
