"""CLI for omop-config: initialise, inspect, switch profiles, test connections, configure packages."""

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


def _prompt_resource_config(
    spec: ResourceSpec,
    config: StackConfig,
    *,
    flags: dict[str, str | int | None] | None = None,
    semantic_name_override: str | None = None,
) -> tuple[str, ConnectionConfig, ResourceConfig] | None:
    """Prompt to create or update a connection + resource for a ResourceSpec.

    When *flags* is provided, flag values skip the corresponding prompts and
    the "keep existing?" confirmation is suppressed. This is the non-interactive
    configuration of the package and is suitable for scripted /
    Docker Compose use.  Omitting *flags* preserves fully-interactive behaviour.

    Parameters
    ----------
    spec
        The ResourceSpec describing the resource to configure.
    config
        The current StackConfig, used to look up existing config and determine defaults.
    flags
        Optional dict of flag values to override prompts.
        Keys correspond to the fields of ConnectionConfig and ResourceConfig.
    semantic_name_override
        When provided, the resource is created/updated under this name instead
        of ``spec.semantic_name``. Used by ``--resource-name`` to create a
        second instance of the same resource type (e.g. ``cdm_db_prod``).

    Returns ``(conn_name, conn, resource)`` or ``None`` to keep existing config.
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

    # Collect already-stored values so re-running configure never re-prompts for
    # fields that are already configured and not explicitly overridden by a flag.
    # Resolution order: explicit flag > stored config > prompt.
    _stored: dict[str, str] = {}
    if existing:
        _stored.update({k: str(v) for k, v in existing.model_dump(exclude_none=True).items()})
        _stored["conn_name"] = existing.primary_db
        _ec = config.connections.get(existing.primary_db)
        if _ec:
            _stored.update({k: str(v) for k, v in _ec.model_dump(exclude_none=True).items()})

    def _v(key: str, label: str, default: str, *, hide_input: bool = False) -> str:
        if (val := flags.get(key)) is not None:
            return str(val)
        if key in _stored:
            return _stored[key]
        return typer.prompt(label, default=default, hide_input=hide_input)

    conn_name = _v("conn_name", "Connection name  (a short label, e.g. 'cdm' or 'prod')", spec.connection_name_hint or "cdm")
    dialect = _v("dialect", "Dialect  (SQLAlchemy driver string, e.g. postgresql+psycopg, sqlite)", "postgresql+psycopg")
    host = _v("host", "Host  (e.g. Docker container name; leave blank for SQLite or socket connections)", "localhost") or None

    port_str = _v("port", "Port  (leave blank to use the dialect default)", "")
    port: int | None = int(port_str) if port_str.strip() else None

    user = _v("user", "User  (leave blank if not required)", "") or None
    password = _v("password", "Password  (leave blank if not required)", "", hide_input=True) or None
    database = _v("database", "Database  (database name, or file path for SQLite)", "") or None

    conn = ConnectionConfig(dialect=dialect, host=host, port=port, user=user, password=password, database=database)

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
        primary_db=conn_name,
        cdm_schema=cdm_schema,
        vocab_schema=vocab_schema,
        results_schema=results_schema,
    )

    return conn_name, conn, resource


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


@app.command()
def configure(  # noqa: PLR0913
    package: Annotated[
        str,
        typer.Argument(help="Package name to configure, e.g. omop_alchemy. Omit to list registered packages."),
    ] = "",
    conn_name: Annotated[str | None, typer.Option("--conn-name", help="Connection label (skips prompt).")] = None,
    dialect: Annotated[str | None, typer.Option("--dialect", help="SQLAlchemy dialect, e.g. postgresql+psycopg (skips prompt).")] = None,
    host: Annotated[str | None, typer.Option("--host", help="Database host (skips prompt).")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Database port (skips prompt).")] = None,
    user: Annotated[str | None, typer.Option("--user", help="Database user (skips prompt).")] = None,
    password: Annotated[str | None, typer.Option("--password", help="Database password (skips prompt).")] = None,
    database: Annotated[str | None, typer.Option("--database", help="Database name (skips prompt).")] = None,
    cdm_schema: Annotated[str | None, typer.Option("--cdm-schema", help="CDM schema name (skips prompt).")] = None,
    vocab_schema: Annotated[str | None, typer.Option("--vocab-schema", help="Vocab schema; omit to share CDM schema (skips prompt).")] = None,
    results_schema: Annotated[str | None, typer.Option("--results-schema", help="Results schema; omit if unused (skips prompt).")] = None,
    resource_name: Annotated[
        str | None,
        typer.Option(
            "--resource-name",
            help=(
                "Create or update the resource under this name instead of the package default. "
                "Use to add a second instance of the same resource type, e.g. "
                "'omop-config configure omop_alchemy --resource-name cdm_db_prod'."
            ),
        ),
    ] = None,
    set_fields: Annotated[
        list[str],
        typer.Option("--set", help="Set an extra field as key=value (e.g. --set ollama_api_base=http://ollama:11434/v1). Repeatable. Forces non-interactive mode for extras."),
    ] = [],
) -> None:
    """Configure a package's [tools.<name>] section.

    Run without flags for interactive prompts (local dev).  Pass connection
    flags to skip all prompts; useful for scripted / Docker Compose use:

        omop-config configure omop_alchemy \\
            --conn-name cdm --dialect postgresql+psycopg \\
            --host db --port 5432 --user omop --password secret \\
            --database omop_cdm --cdm-schema omop

    To add a second resource of the same type (e.g. a production CDM alongside
    a local one) use --resource-name:

        omop-config configure omop_alchemy --resource-name cdm_db_prod

    Packages register support via the 'omop.config' entry-point group in their
    pyproject.toml.
    """
    eps = entry_points(group="omop.config")
    registered = {ep.name: ep for ep in eps}

    raw_flags = {
        "conn_name": conn_name, "dialect": dialect, "host": host, "port": port,
        "user": user, "password": password, "database": database,
        "cdm_schema": cdm_schema, "vocab_schema": vocab_schema, "results_schema": results_schema,
    }
    flags_arg: dict[str, str | int | None] | None = (
        {k: v for k, v in raw_flags.items() if v is not None} or None
    )
    set_dict: dict[str, str] = {}
    for kv in set_fields:
        k, _, v = kv.partition("=")
        if k.strip():
            set_dict[k.strip()] = v

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

    # Configure each resource this package owns (connection + schema setup).
    # When --resource-name is given and the package owns exactly one resource,
    # that resource is created under the override name instead of the default.
    for spec in cls.owned_resources:
        effective_name = resource_name if (resource_name and len(cls.owned_resources) == 1) else None
        result = _prompt_resource_config(spec, config, flags=flags_arg, semantic_name_override=effective_name)
        if result is not None:
            r_conn_name, new_conn, new_resource = result
            config.connections[r_conn_name] = new_conn
            save_name = effective_name or spec.semantic_name
            config.resources[save_name] = new_resource

    # Warn about required resources that are neither owned nor yet configured
    owned_names = {spec.semantic_name for spec in cls.owned_resources}
    for rname in cls.required_resources:
        if rname in owned_names:
            continue
        resolved = config.resource_aliases.get(rname, rname)
        if resolved not in config.resources:
            console.print(
                f"\n[yellow]Warning:[/yellow] Required resource {rname!r} is not configured. "
                f"It may be provided by another package. Run that package's configure command."
            )

    # Load current extras gracefully (resource may not exist yet on a fresh install)
    try:
        current = cls.from_stack(config)
        current_dict = current.to_extra_dict()
    except ConfigurationError:
        current_dict = {}

    # Prompt for package-specific extra fields (skipped in non-interactive mode)
    model_fields = {k: v for k, v in cls.model_fields.items() if k != "tool_name"}
    if model_fields and flags_arg is None and not set_dict:
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
        extra = {**current_dict, **set_dict}

    # Resolve default_resource: prompt when genuinely ambiguous (multiple relevant resources).
    # Filter to resources this package actually uses (owned + required + any --resource-name
    # override) to avoid showing unrelated resources.
    existing_tool = config.tools.get(cls.tool_name)
    existing_default = existing_tool.default_resource if existing_tool else None
    available_resources = sorted(config.resource_names())
    relevant_names = {s.semantic_name for s in cls.owned_resources} | set(cls.required_resources)
    if resource_name:
        relevant_names.add(resource_name)
    relevant_resources = [r for r in available_resources if r in relevant_names]

    if len(relevant_resources) > 1 and flags_arg is None:
        # Multiple relevant resources in interactive mode — ask which one to use.
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
        # Non-interactive with multiple resources: honour explicit --resource-name, else keep existing.
        new_default_resource = resource_name or existing_default or relevant_resources[0]
    else:
        # Single (or zero) relevant resource — set silently.
        new_default_resource = existing_default or (relevant_resources[0] if relevant_resources else None)

    config.tools[cls.tool_name] = ToolConfig(default_resource=new_default_resource, extra=extra)
    save_stack_config(config)
    console.print(f"\n[green]✓[/green] Saved [tools.{cls.tool_name}] to [dim]{DEFAULT_CONFIG_PATH}[/dim]")
