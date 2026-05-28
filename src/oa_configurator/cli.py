"""CLI for omop-config — initialise, inspect, switch profiles, test connections, configure packages."""

from __future__ import annotations

import time
from importlib.metadata import entry_points
from typing import Annotated

import rich
import typer
from rich.console import Console
from rich.table import Table

from .io import patch_active_profile, save_stack_config, write_env_file
from .loader import DEFAULT_CONFIG_PATH, load_stack_config
from .logging_config import configure_logging
from .models import ConnectionConfig, ResourceConfig, StackConfig, ToolConfig
from .package_base import ConfigurationError, ResourceSpec
from .resolver import Resolver

app = typer.Typer(name="omop-config", no_args_is_help=True, add_completion=False)
console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Global callback — verbosity
# ---------------------------------------------------------------------------


@app.callback()
def _main(
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose", "-v",
            count=True,
            help="Increase log verbosity (-v INFO, -vv DEBUG).",
        ),
    ] = 0,
) -> None:
    configure_logging(verbosity=verbose)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command()
def init(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite existing config without prompting."),
    ] = False,
) -> None:
    """Create ~/.config/omop/config.toml (empty). Use 'omop-config configure <pkg>' to populate it."""
    if DEFAULT_CONFIG_PATH.exists() and not force:
        overwrite = typer.confirm(
            f"Config already exists at {DEFAULT_CONFIG_PATH}. Overwrite?",
            default=False,
        )
        if not overwrite:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(0)

    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    save_stack_config(StackConfig())
    console.print(f"[green]✓[/green] Created [dim]{DEFAULT_CONFIG_PATH}[/dim]")

    eps = entry_points(group="omop.config")
    if eps:
        console.print("\nRun configure for each installed package:")
        for ep in sorted(eps, key=lambda e: e.name):
            console.print(f"  omop-config configure {ep.name}")
    else:
        console.print("\nNo packages registered yet — install a package that supports oa_configurator.")


# ---------------------------------------------------------------------------
# _prompt_resource_config (private helper)
# ---------------------------------------------------------------------------


def _prompt_resource_config(
    spec: ResourceSpec,
    config: StackConfig,
) -> tuple[str, ConnectionConfig, ResourceConfig] | None:
    """Prompt to create or update a connection + resource for a ResourceSpec.

    Returns ``(conn_name, conn, resource)`` if the user provides new values,
    or ``None`` if they choose to keep an existing configuration unchanged.
    """
    resolved = config.resource_aliases.get(spec.semantic_name, spec.semantic_name)
    existing = config.resources.get(resolved)
    if existing:
        keep = typer.confirm(
            f"{spec.display_name} is already configured (resource: {resolved!r}). Keep it?",
            default=True,
        )
        if keep:
            return None

    console.print(f"\n[bold]{spec.display_name}[/bold]")
    console.print(f"[dim]{spec.description}[/dim]")
    console.print("[dim]Tip: the value shown in [brackets] is the default — press Enter to accept it.[/dim]\n")

    conn_name = typer.prompt(
        "Connection name  (a short label, e.g. 'cdm' or 'prod')",
        default=spec.connection_name_hint or "cdm",
    )
    dialect = typer.prompt("Dialect  (SQLAlchemy driver string, e.g. postgresql+psycopg2, sqlite)")
    host = typer.prompt("Host  (leave blank for SQLite or socket connections)", default="localhost") or None
    port_str = typer.prompt("Port  (leave blank to use the dialect default)", default="")
    port: int | None = int(port_str) if port_str.strip() else None
    user = typer.prompt("User  (leave blank if not required)", default="") or None
    password = typer.prompt("Password  (leave blank if not required)", default="", hide_input=True) or None
    database = typer.prompt("Database  (database name, or file path for SQLite)", default="") or None

    conn = ConnectionConfig(
        dialect=dialect,
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
    )

    console.print("\n[dim]Schema configuration[/dim]\n")
    cdm_schema = typer.prompt("CDM schema  (schema containing the OMOP tables)", default="omop")
    vocab_schema_str = typer.prompt("Vocab schema  (leave blank to share the CDM schema)", default="")
    results_schema_str = typer.prompt("Results schema  (leave blank to skip)", default="")

    resource = ResourceConfig(
        primary_db=conn_name,
        cdm_schema=cdm_schema,
        vocab_schema=vocab_schema_str or None,
        results_schema=results_schema_str or None,
    )

    return conn_name, conn, resource


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@app.command()
def show(
    profile: Annotated[
        str | None,
        typer.Option("--profile", "-p", help="Activate this profile for the shown output."),
    ] = None,
) -> None:
    """Print the resolved configuration as JSON."""
    try:
        config = load_stack_config()
    except FileNotFoundError:
        err_console.print(f"[red]Config file not found:[/red] {DEFAULT_CONFIG_PATH}")
        err_console.print("Run [bold]omop-config init[/bold] to create it.")
        raise typer.Exit(1)
    if profile is not None:
        config.active_profile = profile
    rich.print_json(config.model_dump_json(exclude_none=True, indent=2))


# ---------------------------------------------------------------------------
# use
# ---------------------------------------------------------------------------


@app.command()
def use(
    profile: Annotated[str, typer.Argument(help="Profile name to activate.")],
) -> None:
    """Set the active profile in config.toml and re-export config.env."""
    try:
        config = load_stack_config()
    except FileNotFoundError:
        err_console.print(f"[red]Config file not found:[/red] {DEFAULT_CONFIG_PATH}")
        raise typer.Exit(1)

    if profile not in config.profiles and profile != "default":
        err_console.print(
            f"[yellow]Warning:[/yellow] profile {profile!r} not found in config — "
            "it will be set anyway. Add connections/resources to it when ready."
        )

    patch_active_profile(profile)
    console.print(f"[green]✓[/green] Active profile set to [bold]{profile}[/bold]")

    config.active_profile = profile
    try:
        env_path = write_env_file(Resolver(config))
        console.print(f"[green]✓[/green] Exported [dim]{env_path}[/dim]")
    except Exception as exc:
        err_console.print(f"[yellow]Warning:[/yellow] Could not export config.env: {exc}")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@app.command()
def verify(
    profile: Annotated[
        str | None,
        typer.Option("--profile", "-p", help="Profile to test."),
    ] = None,
) -> None:
    """Test all configured connections and report status."""
    try:
        config = load_stack_config()
    except FileNotFoundError:
        err_console.print(f"[red]Config file not found:[/red] {DEFAULT_CONFIG_PATH}")
        raise typer.Exit(1)

    if profile is not None:
        config.active_profile = profile

    if not config.connections:
        console.print("[yellow]No connections configured.[/yellow]")
        return

    resolver = Resolver(config)
    table = Table("Connection", "URL", "Status", "Latency")
    all_ok = True

    for name in sorted(config.connections):
        target = resolver.resolve_connection(name)
        try:
            t0 = time.monotonic()
            engine = target.create_engine()
            with engine.connect():
                pass
            elapsed = (time.monotonic() - t0) * 1000
            table.add_row(name, target.safe_url, "[green]OK[/green]", f"{elapsed:.0f} ms")
        except Exception as exc:
            table.add_row(name, target.safe_url, "[red]FAIL[/red]", str(exc)[:60])
            all_ok = False

    console.print(table)
    if not all_ok:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# export-env
# ---------------------------------------------------------------------------


@app.command("export-env")
def export_env(
    profile: Annotated[
        str | None,
        typer.Option("--profile", "-p", help="Profile to use for export."),
    ] = None,
) -> None:
    """Write ~/.config/omop/config.env for Docker Compose env_file:."""
    try:
        config = load_stack_config()
    except FileNotFoundError:
        err_console.print(f"[red]Config file not found:[/red] {DEFAULT_CONFIG_PATH}")
        raise typer.Exit(1)

    if profile is not None:
        config.active_profile = profile

    env_path = write_env_file(Resolver(config))
    console.print(f"[green]✓[/green] Wrote [dim]{env_path}[/dim]")


# ---------------------------------------------------------------------------
# configure
# ---------------------------------------------------------------------------


@app.command()
def configure(
    package: Annotated[
        str,
        typer.Argument(
            help="Package name to configure, e.g. omop_emb. Omit to list registered packages."
        ),
    ] = "",
) -> None:
    """Interactively configure a package's [tools.<name>] section.

    Packages register support via the 'omop.config' entry-point group in their
    pyproject.toml.
    """
    eps = entry_points(group="omop.config")
    registered = {ep.name: ep for ep in eps}

    if not package:
        if not registered:
            console.print("[yellow]No packages registered under 'omop.config' entry points.[/yellow]")
            console.print(
                "\nPackages add support in their pyproject.toml:\n"
                '  [project.entry-points."omop.config"]\n'
                '  omop_emb = "omop_emb.config:OmopEmbConfig"'
            )
        else:
            console.print("[bold]Registered packages:[/bold]")
            for name in sorted(registered):
                console.print(f"  • {name}")
        return

    if package not in registered:
        err_console.print(
            f"[red]Package {package!r} not registered.[/red] "
            f"Available: {', '.join(sorted(registered)) or 'none'}"
        )
        raise typer.Exit(1)

    cls = registered[package].load()

    try:
        config = load_stack_config()
    except FileNotFoundError:
        DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config = StackConfig()

    console.print(f"\n[bold]Configuring [cyan]{package}[/cyan][/bold]")
    console.print(f"[dim]TOML section: [tools.{cls.tool_name}][/dim]")

    # 1. Prompt for each resource this package owns (connection + schema setup)
    for spec in cls.owned_resources:
        result = _prompt_resource_config(spec, config)
        if result is not None:
            conn_name, new_conn, new_resource = result
            config.connections[conn_name] = new_conn
            config.resources[spec.semantic_name] = new_resource

    # 2. Warn about required resources that are neither owned nor yet configured
    owned_names = {spec.semantic_name for spec in cls.owned_resources}
    for rname in cls.required_resources:
        if rname in owned_names:
            continue
        resolved = config.resource_aliases.get(rname, rname)
        if resolved not in config.resources:
            console.print(
                f"\n[yellow]Warning:[/yellow] Required resource {rname!r} is not configured. "
                f"It may be provided by another package — run that package's configure command."
            )

    # 3. Load current extras gracefully (resource may not exist yet on a fresh install)
    try:
        current = cls.from_stack(config)
        current_dict = current.to_extra_dict()
    except ConfigurationError:
        current_dict = {}

    # 4. Prompt for package-specific extra fields
    model_fields = {k: v for k, v in cls.model_fields.items() if k != "tool_name"}
    if model_fields:
        console.print()
        if current_dict:
            console.print("[dim]Current values:[/dim]")
            for k, v in current_dict.items():
                console.print(f"  {k} = {v!r}")
            console.print()

        updated: dict = {}
        for field_name, field_info in model_fields.items():
            default = current_dict.get(field_name, field_info.default)
            desc = field_info.description or ""
            prompt_label = f"{field_name}" + (f" ({desc})" if desc else "")
            raw = typer.prompt(prompt_label, default=str(default) if default is not None else "")
            updated[field_name] = None if raw in ("", "None") else raw

        extra = {k: v for k, v in updated.items() if v is not None}
    else:
        extra = {}

    # 5. Prompt for default_resource only when there is ambiguity: more than
    #    one resource is configured, or an explicit override already exists.
    #    In the normal happy path, owned_resources stored under the canonical
    #    name — no override needed.
    existing_tool = config.tools.get(cls.tool_name)
    available_resources = sorted(config.resource_names())
    existing_default = existing_tool.default_resource if existing_tool else None
    needs_prompt = len(available_resources) > 1 or existing_default is not None

    new_default_resource: str | None = existing_default
    if cls.required_resources and needs_prompt:
        console.print(f"\n[dim]Available resources: {', '.join(available_resources)}[/dim]")
        new_default_resource = (
            typer.prompt(
                "Resource  (which resource should this package use)",
                default=existing_default or cls.required_resources[0],
            )
            or None
        )

    # 6. Save
    config.tools[cls.tool_name] = ToolConfig(default_resource=new_default_resource, extra=extra)
    save_stack_config(config)
    console.print(f"\n[green]✓[/green] Saved [tools.{cls.tool_name}] to [dim]{DEFAULT_CONFIG_PATH}[/dim]")
