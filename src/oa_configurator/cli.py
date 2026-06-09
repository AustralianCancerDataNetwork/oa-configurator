"""CLI for omop-config: initialise, inspect, switch profiles, test connections, configure packages."""

from __future__ import annotations

import time
from importlib.metadata import entry_points
from typing import Annotated

import click
import rich
import typer
from typer.core import TyperGroup
from rich.console import Console
from rich.table import Table

from .io import patch_active_profile, save_stack_config, write_env_file
from .loader import DEFAULT_CONFIG_PATH, load_stack_config
from .logging_config import configure_logging
from .models import DatabaseConfig, ResourceConfig, StackConfig, ToolConfig
from .package_base import ConfigurationError, ResourceSpec
from .resolver import Resolver

app = typer.Typer(name="omop-config", no_args_is_help=True, add_completion=False)
console = Console()
err_console = Console(stderr=True)


@app.callback()  # required by Typer to attach global --verbose/-v before any subcommand
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
        console.print("\nNo packages registered yet. Install a package that supports oa_configurator.")


def _resolve_resource(
    spec: ResourceSpec,
    config: StackConfig,
    *,
    flags: dict[str, str] | None = None,
    semantic_name_override: str | None = None,
) -> tuple[str, DatabaseConfig, ResourceConfig] | None:
    """Resolve or create a database config + resource for a ResourceSpec.

    Resolution order for each field: explicit flag > stored config > prompt.

    When *flags* is provided, the "keep existing?" confirmation is suppressed.
    This is the non-interactive path suitable for scripted / Docker Compose use.
    Omitting *flags* preserves fully-interactive behaviour.

    Parameters
    ----------
    spec
        The ResourceSpec describing the resource to configure.
    config
        The current StackConfig, used to look up existing config and determine defaults.
    flags
        Optional dict of flag values to override prompts.
        Keys correspond to the fields of DatabaseConfig and ResourceConfig.
    semantic_name_override
        When provided, the resource is created/updated under this name instead
        of ``spec.semantic_name``. Used by ``--resource-name`` to create a
        second instance of the same resource type (e.g. ``cdm_db_prod``).

    Returns ``(db_name, db_config, resource)`` or ``None`` to keep existing config.
    """
    non_interactive = flags is not None
    flags = flags or {}
    resource_name = semantic_name_override or spec.semantic_name
    resolved = config.resource_aliases.get(resource_name, resource_name)
    existing = config.resources.get(resolved)

    if existing and not non_interactive:
        if typer.confirm(
            f"{spec.display_name} is already configured (resource: {resource_name!r}). Keep it?",
            default=True,
        ):
            return None

    if not non_interactive:
        console.print(f"\n[bold]{spec.display_name}[/bold]")
        console.print(f"[dim]{spec.description}[/dim]")
        console.print("[dim]Tip: the value shown in [brackets] is the default. Press Enter to accept it.[/dim]\n")

    # Resolution order: explicit flag > stored config > prompt.
    # _stored holds every field from the existing config as strings ("" for None)
    # so _v can find them and skip the prompt. Callers do `_v(...) or None` to
    # convert empty-string back to None for optional fields.
    _stored: dict[str, str] = {}
    if existing:
        _stored.update({k: str(v) if v is not None else "" for k, v in existing.model_dump().items()})
        _edb = config.databases.get(existing.database)
        if _edb:
            _stored.update({k: str(v) if v is not None else "" for k, v in _edb.model_dump().items()})

    def _v(key: str, label: str, default: str, *, hide_input: bool = False) -> str:
        if (val := flags.get(key)) is not None:
            return str(val)
        if key in _stored:
            return _stored[key]
        return typer.prompt(label, default=default, hide_input=hide_input)

    database = _v("database", "Database label  (short name for this database entry, e.g. 'cdm')", spec.connection_name_hint or "cdm")
    dialect = _v("dialect", "Dialect  (SQLAlchemy driver string, e.g. postgresql+psycopg, sqlite)", "postgresql+psycopg")
    host = _v("host", "Host  (e.g. Docker container name; leave blank for SQLite or socket connections)", "localhost") or None

    port_str = _v("port", "Port  (leave blank to use the dialect default)", "")
    port: int | None = int(port_str) if port_str.strip() else None

    user = _v("user", "User  (leave blank if not required)", "") or None
    password = _v("password", "Password  (leave blank if not required)", "", hide_input=True) or None
    database_name = _v("database_name", "Database name  (name of the database on the server, or file path for SQLite)", "") or None

    db_config = DatabaseConfig(dialect=dialect, host=host, port=port, user=user, password=password, database_name=database_name)

    if not non_interactive:
        console.print("\n[dim]Schema configuration[/dim]\n")

    if spec.is_cdm_database:
        cdm_schema = _v("cdm_schema", "CDM schema  (schema containing the OMOP tables)", spec.cdm_schema_default)
        vocab_schema = _v("vocab_schema", "Vocab schema  (blank = same schema as CDM; set only if vocabulary lives in a separate schema)", "") or None
        results_schema = _v("results_schema", "Results schema  (for Achilles/Atlas results tables; blank = not used)", "") or None
    else:
        cdm_schema = _v("cdm_schema", "Schema  (leave blank for public/default)", spec.cdm_schema_default)
        vocab_schema = None
        results_schema = None

    resource = ResourceConfig(
        database=database,
        cdm_schema=cdm_schema,
        vocab_schema=vocab_schema,
        results_schema=results_schema,
    )

    return database, db_config, resource


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
            f"[yellow]Warning:[/yellow] profile {profile!r} not found in config.toml, but"
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

    if not config.databases:
        console.print("[yellow]No databases configured.[/yellow]")
        return

    resolver = Resolver(config)
    table = Table("Database", "URL", "Status", "Latency")
    all_ok = True

    for name in sorted(config.databases):
        target = resolver.resolve_database(name)
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


def _resolve_extra_fields(
    cls,
    config: StackConfig,
    *,
    set_dict: dict[str, str],
    interactive: bool,
) -> dict:
    """Resolve package-specific extra fields using flag (--set) → stored → prompt."""
    try:
        current = cls.from_stack(config)
        current_dict = current.to_extra_dict()
    except ConfigurationError:
        current_dict = {}

    extra: dict = {}
    for field_name, field_info in cls.model_fields.items():
        if field_name == "tool_name":
            continue
        if field_name in set_dict:
            extra[field_name] = set_dict[field_name]
        elif field_name in current_dict:
            extra[field_name] = current_dict[field_name]
        elif interactive:
            desc = field_info.description or ""
            label = f"{field_name}" + (f"  ({desc})" if desc else "")
            raw = typer.prompt(label, default=str(field_info.default) if field_info.default is not None else "")
            if raw and raw != "None":
                extra[field_name] = raw
    return extra


class _DynamicConfigureGroup(TyperGroup):
    def __init__(self, **kwargs):
        kwargs.setdefault("invoke_without_command", True)
        super().__init__(**kwargs)

    def list_commands(self, ctx):
        return sorted(ep.name for ep in entry_points(group="omop.config"))

    def get_command(self, ctx, cmd_name):
        eps = {ep.name: ep for ep in entry_points(group="omop.config")}
        ep = eps.get(cmd_name)
        return _build_package_command(cmd_name, ep.load()) if ep else None


def _build_resource_params(spec: ResourceSpec) -> list[click.Parameter]:
    """Generate Click options for one resource from DatabaseConfig + ResourceConfig fields."""
    params: list[click.Parameter] = []
    for name, info in DatabaseConfig.model_fields.items():
        if name == "read_only":
            continue
        params.append(click.Option(
            [f"--{name.replace('_', '-')}"],
            default=None,
            type=click.STRING,
            help=info.description or "",
        ))
    resource_field_names = ["database", "cdm_schema"]
    if spec.is_cdm_database:
        resource_field_names += ["vocab_schema", "results_schema"]
    for name in resource_field_names:
        info = ResourceConfig.model_fields[name]
        params.append(click.Option(
            [f"--{name.replace('_', '-')}"],
            default=None,
            type=click.STRING,
            help=info.description or "",
        ))
    return params


def _build_extra_params(cls) -> list[click.Parameter]:
    """Generate Click options for a package's extra fields from its model_fields."""
    return [
        click.Option(
            [f"--{name.replace('_', '-')}"],
            default=None,
            type=click.STRING,
            help=info.description or "",
        )
        for name, info in cls.model_fields.items()
    ]


def _build_package_command(ep_name: str, cls) -> click.Command:
    """Build a Click command for one registered package entry point."""
    resource_params = [p for spec in cls.owned_resources for p in _build_resource_params(spec)]
    extra_params = _build_extra_params(cls)
    resource_names = {p.name for p in resource_params}
    extra_names = {p.name for p in extra_params}

    resource_name_opt = click.Option(
        ["--resource-name"],
        default=None,
        help=(
            "Create or update the resource under this name instead of the package default. "
            "Use to add a second instance (e.g. --resource-name cdm_db_prod)."
        ),
    )

    def callback(**kwargs):
        flags_arg = {k: str(v) for k, v in kwargs.items()
                     if k in resource_names and v is not None} or None
        set_dict = {k: str(v) for k, v in kwargs.items()
                    if k in extra_names and v is not None}
        _run_configure_package(cls, flags_arg, set_dict, kwargs.get("resource_name"))

    return click.Command(
        name=ep_name,
        callback=callback,
        params=resource_params + extra_params + [resource_name_opt],
        help=f"Configure {cls.tool_name} settings in config.toml.",
    )


def _list_packages() -> None:
    eps = entry_points(group="omop.config")
    registered = {ep.name: ep for ep in eps}
    if not registered:
        console.print("[yellow]No packages registered under 'omop.config' entry points.[/yellow]")
        console.print(
            "\nPackages add support in their pyproject.toml:\n"
            '  [project.entry-points."omop.config"]\n'
            '  my-package = "my-package.config:MyPackageConfig"'
        )
    else:
        console.print("[bold]Registered packages:[/bold]")
        for name in sorted(registered):
            console.print(f"  • {name}")


def _run_configure_package(
    cls,
    flags_arg: dict[str, str] | None,
    set_dict: dict[str, str],
    resource_name: str | None,
) -> None:
    """Run the configure flow for one package."""
    try:
        config = load_stack_config()
    except FileNotFoundError:
        DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config = StackConfig()

    tool_name = cls.tool_name
    console.print(f"\n[bold]Configuring [cyan]{tool_name}[/cyan][/bold]")
    console.print(f"[dim]TOML section: \\[tools.{tool_name}][/dim]")

    for spec in cls.owned_resources:
        effective_name = resource_name if (resource_name and len(cls.owned_resources) == 1) else None
        result = _resolve_resource(spec, config, flags=flags_arg, semantic_name_override=effective_name)
        if result is not None:
            db_label, new_db, new_resource = result
            config.databases[db_label] = new_db
            save_name = effective_name or spec.semantic_name
            config.resources[save_name] = new_resource

    owned_names = {spec.semantic_name for spec in cls.owned_resources}
    for rname in cls.required_resources:
        if rname in owned_names:
            continue
        resolved_rname = config.resource_aliases.get(rname, rname)
        if resolved_rname not in config.resources:
            console.print(
                f"\n[yellow]Warning:[/yellow] Required resource {rname!r} is not configured. "
                f"It may be provided by another package. Run that package's configure command."
            )

    extra = _resolve_extra_fields(cls, config, set_dict=set_dict, interactive=flags_arg is None)

    existing_tool = config.tools.get(tool_name)
    existing_default = existing_tool.default_resource if existing_tool else None
    available_resources = sorted(config.resource_names())
    relevant_names = {s.semantic_name for s in cls.owned_resources} | set(cls.required_resources)
    if resource_name:
        relevant_names.add(resource_name)
    relevant_resources = [r for r in available_resources if r in relevant_names]

    if len(relevant_resources) > 1 and flags_arg is None:
        console.print(f"\n[dim]Available resources for this package: {', '.join(relevant_resources)}[/dim]")
        prompt_default = resource_name or existing_default or relevant_resources[0]
        new_default_resource = (
            typer.prompt(
                "Default resource  (which resource should this package use)",
                default=prompt_default,
            )
            or None
        )
    elif len(relevant_resources) > 1 and flags_arg is not None:
        new_default_resource = resource_name or existing_default or relevant_resources[0]
    else:
        new_default_resource = existing_default or (relevant_resources[0] if relevant_resources else None)

    config.tools[tool_name] = ToolConfig(default_resource=new_default_resource, extra=extra)
    save_stack_config(config)
    console.print(f"\n[green]✓[/green] Saved \\[tools.{tool_name}] to [dim]{DEFAULT_CONFIG_PATH}[/dim]")


@app.command(name="configure", cls=_DynamicConfigureGroup)  # type: ignore[arg-type]
def configure(ctx: typer.Context) -> None:
    r"""Configure a package's \[tools.<name>] section.

    Run 'omop-config configure <package> --help' to see that package's flags.
    Packages register support via the 'omop.config' entry-point group.
    """
    if ctx.invoked_subcommand is None:
        _list_packages()
