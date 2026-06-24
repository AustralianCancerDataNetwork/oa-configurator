"""CLI for omop-config: initialise, inspect, switch profiles, test connections, configure packages."""

from __future__ import annotations

import time
from functools import partial
from importlib.metadata import entry_points
from typing import Annotated

import click
import rich
import sqlalchemy as sa
import typer
from typer.core import TyperGroup
from rich.console import Console
from rich.table import Table

from .io import patch_active_profile, save_stack_config, write_env_file
from .loader import CONFIG_PATH, load_stack_config
from .logging_config import configure_logging
from .models import DatabaseConfig, ResourceConfig, StackConfig, ToolConfig
from .package_base import ConfigurationError, ResourceSpec
from .resolver import Resolver
from .cli_fields import (
    CDM_SCHEMA_FIELDS,
    DATABASE_LABEL_FIELDS,
    NON_CDM_SCHEMA_FIELDS,
    build_database_config,
    build_resource_config,
    build_resource_params,
    resolve_field_value,
    resolve_fields,
)

app = typer.Typer(name="omop-config", no_args_is_help=True, add_completion=False)
console = Console()
err_console = Console(stderr=True)


def _resolve_resource(
    spec: ResourceSpec,
    config: StackConfig,
    *,
    flags: dict[str, str] | None = None,
    semantic_name_override: str | None = None,
    is_test_resource: bool = False,
) -> tuple[str, DatabaseConfig | None, ResourceConfig] | None:
    """Resolve or create a database config + resource for a ResourceSpec.

    Parameters
    ----------
    spec : ResourceSpec
        The ResourceSpec describing the resource to resolve.
    config : StackConfig
        The current StackConfig, used to look up existing stored config for 
        this resource and any existing connections.
    flags : dict[str, str], optional
        A dict of flag values for this resource, keyed by field name
        (e.g. "host", "port"). Presence of this argument (even if empty) 
        indicates a non-interactive invocation where no prompts should be shown.
        Non-interactive requires all required fields to be supplied via flags or
        stored config. Missing required fields will be reported.
    semantic_name_override: str, optional
        When given, this name is used instead of spec.semantic_name for looking up
        existing config and for the saved resource name.
    is_test_resource: bool
        Whether this resource is a test resource. If True, the safety checks around
        test resources are applied (see _configure_test_resources). Default: False
    
    Returns
    -------
    db_name : str
        The name of the database config to use for this resource (either existing or new).
    db_config : DatabaseConfig, optional
        The new DatabaseConfig to save for this resource, or None to reuse an existing one.
    resource : ResourceConfig
        The ResourceConfig to save for this resource (either existing with updates or new).
    """
    non_interactive = flags is not None
    flags = flags or {}
    resource_name = semantic_name_override or spec.semantic_name
    resolved = config.resource_aliases.get(resource_name, resource_name)
    existing = config.resources.get(resolved)

    declined = False
    if existing and not non_interactive:
        if typer.confirm(
            f"{spec.display_name} is already configured (resource: {resource_name!r}). Keep it?",
            default=True,
        ):
            return None
        declined = True

    if not non_interactive:
        console.print(f"\n[bold]{spec.display_name}[/bold]")
        console.print(f"[dim]{spec.description}[/dim]")
        console.print("[dim]Tip: the value shown in [brackets] is the default. Press Enter to accept it.[/dim]\n")

    _stored: dict[str, str] = {}
    if existing and not declined:
        _stored.update({k: str(v) if v is not None else "" for k, v in existing.model_dump().items()})
        _edb = config.databases.get(existing.database)
        if _edb:
            _stored.update({k: str(v) if v is not None else "" for k, v in _edb.model_dump().items()})

    missing_required: list[str] = []

    _resolve_field = partial(
        resolve_field_value,
        flags=flags,
        stored=_stored,
        spec_defaults=spec.defaults,
        non_interactive=non_interactive,
        missing_required=missing_required,
    )

    # Offer reuse of an existing connection when none is configured yet.
    reuse_existing_label: str | None = None
    if not existing and not non_interactive and config.databases:
        if is_test_resource:
            candidates = [n for n, db in config.databases.items() if db.test_only]
        else:
            candidates = [n for n, db in config.databases.items() if not db.test_only]
        if candidates:
            hint = spec.connection_name_hint or ""
            suggested = hint if hint in candidates else candidates[0]
            console.print(f"  Existing connections: {', '.join(candidates)}")
            choice = typer.prompt(
                "  Point to an existing connection, or 'new' to create one",
                default=suggested,
            )
            if choice != "new" and choice in candidates:
                reuse_existing_label = choice

    schema_fields = CDM_SCHEMA_FIELDS if spec.is_cdm_database else NON_CDM_SCHEMA_FIELDS

    if reuse_existing_label:
        if not non_interactive:
            console.print("\n[dim]Schema configuration[/dim]\n")
        schema_values = resolve_fields(schema_fields, spec, _resolve_field)
        _check_missing_required(spec, missing_required, is_test_resource, non_interactive)
        resource = build_resource_config(reuse_existing_label, schema_values, spec.is_cdm_database)
        return reuse_existing_label, None, resource

    conn_values = resolve_fields(DATABASE_LABEL_FIELDS, spec, _resolve_field)

    if not non_interactive:
        console.print("\n[dim]Schema configuration[/dim]\n")

    schema_values = resolve_fields(schema_fields, spec, _resolve_field)
    _check_missing_required(spec, missing_required, is_test_resource, non_interactive)

    db_config, database = build_database_config(conn_values)
    resource = build_resource_config(database, schema_values, spec.is_cdm_database)

    return database, db_config, resource


def _check_missing_required(
    spec: ResourceSpec,
    missing_required: list[str],
    is_test_resource: bool,
    non_interactive: bool,
) -> None:
    """Abort with a clear error if a non-interactive resolution left required fields unset.

    Parameters
    ----------
    spec : ResourceSpec
        The resource being resolved, used for the display name in the error message.
    missing_required : list[str]
        Field names appended to by resolve_field_value while resolving spec. Empty
        means nothing is missing.
    is_test_resource : bool
        Whether spec is a test resource, controls the ``--test-`` flag prefix shown
        in the error message.
    non_interactive : bool
        Whether this resolution was non-interactive. Interactive resolutions never
        have anything in missing_required, since prompting always produces a value.
    """
    if not non_interactive or not missing_required:
        return
    prefix = "--test-" if is_test_resource else "--"
    flag_names = ", ".join(f"{prefix}{k.replace('_', '-')}" for k in missing_required)
    err_console.print(
        f"\n[red bold]Missing required field(s) for {spec.display_name!r}:[/red bold] {flag_names}\n"
        f"No flag, stored config, or spec default is available for these. Pass them explicitly."
    )
    raise typer.Exit(1)


def _resolve_extra_fields(
    cls,
    config: StackConfig,
    *,
    set_dict: dict[str, str],
    interactive: bool,
) -> dict:
    """Resolve package-specific extra fields using flag (--set) then stored then prompt.

    Parameters
    ----------
    cls
        The package's PackageConfigBase subclass, whose model_fields are resolved.
    config : StackConfig
        The current StackConfig, used to read any already-stored extras.
    set_dict : dict[str, str]
        Flag values from ``--set``, keyed by field name. Checked first.
    interactive : bool
        Whether to prompt for fields not covered by set_dict or stored config.
        Non-interactively, such fields are simply omitted (they fall back to the
        field's own pydantic default when the config class is loaded).

    Returns
    -------
    dict
        Resolved extra field values, keyed by field name.
    """
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

# ------------------
# Dynamic configure command that discovers packages via entry points and generates a subcommand for each.
# ------------------

class _DynamicConfigureGroup(TyperGroup):
    def __init__(self, **kwargs):
        kwargs.setdefault("invoke_without_command", True)
        super().__init__(**kwargs)

    def list_commands(self, ctx):
        return sorted(ep.name for ep in entry_points(group="omop.config"))

    def get_command(self, ctx, cmd_name):  # type: ignore 
        eps = {ep.name: ep for ep in entry_points(group="omop.config")}
        ep = eps.get(cmd_name)
        return _build_package_command(cmd_name, ep.load()) if ep else None


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
    resource_params = [p for spec in cls.owned_resources for p in build_resource_params(spec)]
    test_resource_params = [
        p for spec in cls.test_resources for p in build_resource_params(spec, prefix="test-")
    ]
    extra_params = _build_extra_params(cls)
    resource_names = {p.name for p in resource_params}
    test_resource_names = {p.name for p in test_resource_params}
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
        test_flags_arg = {
            k[len("test_"):]: str(v) for k, v in kwargs.items()
            if k in test_resource_names and v is not None
        } or None
        set_dict = {k: str(v) for k, v in kwargs.items()
                    if k in extra_names and v is not None}
        _run_configure_package(cls, flags_arg, set_dict, kwargs.get("resource_name"), test_flags_arg)

    return click.Command(
        name=ep_name,
        callback=callback,
        params=resource_params + test_resource_params + extra_params + [resource_name_opt],
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
    test_flags_arg: dict[str, str] | None = None,
) -> None:
    """Run the configure flow for one package.

    Parameters
    ----------
    cls
        The package's PackageConfigBase subclass.
    flags_arg : dict[str, str], optional
        Flag values for the package's owned resource(s), keyed by field name.
        None means no owned-resource flags were given on this invocation.
    set_dict : dict[str, str]
        Flag values for ``--set`` package extras, keyed by field name.
    resource_name : str or None
        Resource name override from ``--resource-name``.
    test_flags_arg : dict[str, str] or None, optional
        Flag values for the package's test resource(s), keyed by field name.
        None means no ``--test-*`` flags were given. Default: None

    Notes
    -----
    Interactivity is decided per resource, not globally. The invocation is
    non-interactive as a whole the moment any of flags_arg, test_flags_arg or
    set_dict is given, but that alone does not mean every resource is
    touched:

    1. The owned resource is only resolved when flags_arg is not None, i.e.
       at least one of its own flags was given. An invocation with only
       ``--test-*`` flags (e.g. a CI job that only needs a test database)
       leaves the owned resource completely untouched, rather than prompting
       for it (no TTY, would abort) or writing defaults nobody asked for.
    2. The test resource is resolved non-interactively whenever
       test_flags_arg is not None. The interactive prompt only fires 
       for a fully bare invocation, with no flags anywhere.
    3. Within a resource that is being resolved, individual missing fields
       still fall back to stored config, a spec default, or a safe default
       value. See _resolve_resource for which fields error out instead.
    """
    try:
        config = load_stack_config()
    except FileNotFoundError:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config = StackConfig()

    tool_name = cls.tool_name
    console.print(f"\n[bold]Configuring [cyan]{tool_name}[/cyan][/bold]")
    console.print(f"[dim]TOML section: \\[tools.{tool_name}][/dim]")


    any_explicit_flags = flags_arg is not None or test_flags_arg is not None or bool(set_dict)

    for spec in cls.owned_resources:
        if flags_arg is None and any_explicit_flags:
            continue
        effective_name = resource_name if (resource_name and len(cls.owned_resources) == 1) else None
        result = _resolve_resource(spec, config, flags=flags_arg, semantic_name_override=effective_name)
        if result is not None:
            db_label, new_db, new_resource = result
            if new_db is not None:
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

    extra = _resolve_extra_fields(
        cls, config, set_dict=set_dict, interactive=flags_arg is None and not any_explicit_flags
    )

    existing_tool = config.tools.get(tool_name)
    existing_default = existing_tool.default_resource if existing_tool else None
    available_resources = sorted(config.resource_names())
    relevant_names = {s.semantic_name for s in cls.owned_resources} | set(cls.required_resources)
    if resource_name:
        relevant_names.add(resource_name)
    relevant_resources = [r for r in available_resources if r in relevant_names]

    if len(relevant_resources) > 1 and flags_arg is None and not any_explicit_flags:
        console.print(f"\n[dim]Available resources for this package: {', '.join(relevant_resources)}[/dim]")
        prompt_default = resource_name or existing_default or relevant_resources[0]
        new_default_resource = (
            typer.prompt(
                "Default resource  (which resource should this package use)",
                default=prompt_default,
            )
            or None
        )
    elif len(relevant_resources) > 1:
        new_default_resource = resource_name or existing_default or relevant_resources[0]
    else:
        new_default_resource = existing_default or (relevant_resources[0] if relevant_resources else None)

    config.tools[tool_name] = ToolConfig(default_resource=new_default_resource, extra=extra)
    save_stack_config(config)
    console.print(f"\n[green]✓[/green] Saved \\[tools.{tool_name}] to [dim]{CONFIG_PATH}[/dim]")

    if cls.test_resources:
        if test_flags_arg is not None:
            _configure_test_resources(cls, config, test_flags_arg)
        elif not any_explicit_flags:
            console.print("\n[dim]─── Test database (optional) ───[/dim]")
            console.print(
                "[yellow]⚠[/yellow]  Test resources are used by the test suite, which runs"
                " DROP SCHEMA CASCADE on every run.\n"
                "   Point to a [bold]dedicated test_only connection[/bold], never to real data.\n"
                "   Existing test_only connections will be offered for reuse."
            )
            want_test = typer.confirm("Configure a test database resource?", default=False)
            if want_test:
                _configure_test_resources(cls, config, None)
        # else: any_explicit_flags is True but no --test-* flags were given.
        # Test resources are opt-in, so leave them untouched.

def _configure_test_resources(
    cls,
    config: StackConfig,
    flags: dict[str, str] | None,
) -> None:
    """Resolve and save every spec in cls.test_resources, applying the test_only safety guard.

    Parameters
    ----------
    cls
        The package's PackageConfigBase subclass, whose test_resources are resolved.
    config : StackConfig
        The current StackConfig, updated in place with each resolved test resource.
    flags : dict[str, str] or None
        ``--test-*`` flag values, keyed by field name (without the test- prefix).
        None means this came from the interactive confirm-driven flow instead.

    Notes
    -----
    Shared by both the interactive confirm-driven flow and the non-interactive
    ``--test-*`` flag flow. The safety checks (test_only marking, production
    collision detection) must hold identically in both.
    """
    test_names = {s.semantic_name for s in cls.test_resources}
    for spec in cls.test_resources:
        result = _resolve_resource(spec, config, flags=flags, is_test_resource=True)
        if result is None:
            continue
        db_label, new_db, new_resource = result

        if new_db is not None:
            # New connection: set test_only flag and check for production collision.
            new_db.test_only = True
            for res_name, existing_res in config.resources.items():
                if res_name in test_names:
                    continue
                existing_conn = config.databases.get(existing_res.database)
                if existing_conn is None or existing_conn.test_only:
                    continue
                if (
                    existing_conn.host == new_db.host
                    and existing_conn.database_name == new_db.database_name
                    and existing_conn.port == new_db.port
                ):
                    err_console.print(
                        f"\n[red bold]DANGER[/red bold]: these connection details match the"
                        f" [bold]{res_name!r}[/bold] resource (same host and database name).\n"
                        f"Tests run DROP SCHEMA CASCADE, which would destroy your data.\n"
                        f"Use a different [bold]host[/bold] or [bold]database name[/bold]."
                    )
                    raise typer.Exit(1)
            config.databases[db_label] = new_db
        else:
            # Reuse: verify the referenced connection is actually test_only.
            existing_db = config.databases.get(db_label)
            if existing_db is not None and not existing_db.test_only:
                err_console.print(
                    f"\n[red bold]DANGER[/red bold]: the connection {db_label!r} is not"
                    f" marked test_only=true. Point test resources only to test_only"
                    f" connections.\n"
                )
                raise typer.Exit(1)

        config.resources[spec.semantic_name] = new_resource
        save_stack_config(config)
        console.print(
            f"[green]✓[/green] Test resource [bold]{spec.semantic_name!r}[/bold] written."
        )

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

    eps = entry_points(group="omop.config")
    if eps:
        console.print("\nRun configure for each installed package:")
        for ep in sorted(eps, key=lambda e: e.name):
            console.print(f"  omop-config configure {ep.name}")
    else:
        console.print("\nNo packages registered yet. Install a package that supports oa_configurator.")

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
        err_console.print(f"[red]Config file not found:[/red] {CONFIG_PATH}")
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
        err_console.print(f"[red]Config file not found:[/red] {CONFIG_PATH}")
        raise typer.Exit(1)

    if profile not in config.profiles and profile != "default":
        err_console.print(
            f"[yellow]Warning:[/yellow] profile {profile!r} not found in config.toml, but"
            " it will be set anyway. Add connections/resources to it when ready."
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
        err_console.print(f"[red]Config file not found:[/red] {CONFIG_PATH}")
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
def export_env(
    profile: Annotated[
        str | None,
        typer.Option("--profile", "-p", help="Profile to use for export."),
    ] = None,
) -> None:
    """Write CONFIG_PATH's sibling .env file (default ~/.config/omop/config.env) for Docker Compose env_file:."""
    try:
        config = load_stack_config()
    except FileNotFoundError:
        err_console.print(f"[red]Config file not found:[/red] {CONFIG_PATH}")
        raise typer.Exit(1)

    if profile is not None:
        config.active_profile = profile

    env_path = write_env_file(Resolver(config))
    console.print(f"[green]✓[/green] Wrote [dim]{env_path}[/dim]")


@app.command(name="configure", cls=_DynamicConfigureGroup)  # type: ignore[arg-type]
def configure(ctx: typer.Context) -> None:
    r"""Configure a package's \[tools.<name>] section.

    Run 'omop-config configure <package> --help' to see that package's flags.
    Packages register support via the 'omop.config' entry-point group.
    """
    if ctx.invoked_subcommand is None:
        _list_packages()
