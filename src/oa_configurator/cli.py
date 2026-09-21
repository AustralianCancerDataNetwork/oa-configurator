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

from .cli_support import _build_entry_params, _save_stack_config_or_exit
from .domains.llm.cli import models_app, providers_app
from .domains.resources.cli import connections_app, databases_app
from .domains.resources.rectify import drop_orphan_schema_tables
from .domains.resources.schema import ResolvedCDMDatabase, ResolvedDatabase, Role
from .domains.resources.sql import (
    SchemaDriftError,
    guard_schema_provenance,
    record_schema_provenance,
    Dialect,
)
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
    _save_stack_config_or_exit(StackConfig(), save=save_stack_config)
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
    rich.print_json(config.masked_json(exclude_none=True, indent=2))


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

    schema_table = Table("Database", "Role", "Schema", "Status", "Detail")
    schema_ok = True
    for db_name in sorted(config.databases):
        try:
            resolved = resolver.resolve_database(db_name)
        except Exception as exc:
            schema_table.add_row(db_name, "-", "-", "[red]FAIL[/red]", str(exc)[:60])
            schema_ok = False
            continue
        for role in _roles_for(resolved):
            schema_ok &= _verify_schema_provenance(schema_table, resolved, role, label=db_name)

    for vs_name in sorted(config.vector_stores):
        try:
            resolved_vs = resolver.resolve_vector_store(vs_name)
        except Exception as exc:
            schema_table.add_row(vs_name, "-", "-", "[red]FAIL[/red]", str(exc)[:60])
            schema_ok = False
            continue
        schema_ok &= _verify_schema_provenance(
            schema_table, resolved_vs.database, Role.PRIMARY, label=vs_name
        )

    if schema_table.row_count:
        console.print(schema_table)

    if not all_ok or not schema_ok:
        raise typer.Exit(1)


def _roles_for(resolved: ResolvedDatabase) -> tuple[Role, ...]:
    if isinstance(resolved, ResolvedCDMDatabase):
        return (Role.PRIMARY, Role.VOCAB, Role.RESULTS)
    return (Role.PRIMARY,)


def _verify_schema_provenance(
    table: Table, resolved: ResolvedDatabase, role: Role, *, label: str
) -> bool:
    """Open guard_schema_provenance() with an empty body: the exact same
    check the DDL-time gate uses, no DDL run, refreshing last_verified_at
    on success as a side effect. Adds one row to table (whose columns are
    the Table("Database", "Role", ...) headers passed by the caller, in
    that order), returns whether it passed.
    """
    target_connection = resolved.connection_target(role)
    try:
        engine = target_connection.create_engine()
        try:
            with engine.begin() as connection, guard_schema_provenance(
                connection, resolved, role=role
            ):
                pass
        finally:
            engine.dispose()
    except SchemaDriftError as exc:
        table.add_row(label, role.value, "?", "[red]DRIFT[/red]", str(exc)[:80])
        return False
    except Exception as exc:
        table.add_row(label, role.value, "?", "[red]FAIL[/red]", str(exc)[:60])
        return False
    table.add_row(label, role.value, "-", "[green]OK[/green]", "")
    return True


@app.command("acknowledge-schema-migration")
def acknowledge_schema_migration(
    database: Annotated[str, typer.Option("--database", help="Name of the [databases.*] entry to acknowledge.")],
    reason: Annotated[
        str,
        typer.Option(
            "--reason",
            help="Free-text justification for this acknowledgment. Mandatory: there is no --yes shortcut.",
        ),
    ],
    new_schema: Annotated[
        str | None,
        typer.Option(
            "--new-schema",
            help="Schema to record as the accepted baseline. Omit to target the "
            "default/unqualified schema (e.g. SQLite, or a dialect's own default schema).",
        ),
    ] = None,
    role: Annotated[
        Role, typer.Option("--role", help="Logical role whose schema is being acknowledged.")
    ] = Role.PRIMARY,
) -> None:
    """Record a schema as the deliberate baseline for a database/role.

    Overwrites any existing provenance row (its prior value moves to
    previous_schema); does not touch the CDM tables themselves. Generic
    over any [databases.*] entry, not tied to any particular domain
    package. This is the one CLI-level remediation path for the schema-drift
    check every configured database already gets from `verify`.
    """
    try:
        stack = load_stack_config()
        resolved = Resolver(stack).resolve_database(database)
        engine = resolved.create_engine(role=role)
        try:
            with engine.begin() as connection:
                record_schema_provenance(
                    connection, resolved, role=role, new_schema=new_schema, reason=reason
                )
        finally:
            engine.dispose()
    except FileNotFoundError:
        err_console.print(f"[red]Config file not found:[/red] {CONFIG_PATH}")
        raise typer.Exit(1)
    except Exception as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    console.print(
        f"[green]Acknowledged[/green] {database!r} (role {role.value!r}) -> schema {new_schema!r}."
    )


@app.command("drop-orphan-schema-tables")
def drop_orphan_schema_tables_command(
    database: Annotated[
        str, typer.Option("--database", help="Name of the [databases.*] entry providing the connection.")
    ],
    schema: Annotated[str, typer.Option("--schema", help="Orphan schema to inspect/drop tables from.")],
    role: Annotated[
        Role, typer.Option("--role", help="Logical role providing the connection to use.")
    ] = Role.PRIMARY,
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Actually drop the previewed tables. Omit to preview only.")
    ] = False,
) -> None:
    """Drop tables physically found in an orphaned schema, after a stack-wide safety check.

    Refuses if the named schema is still the current schema target of any
    configured database/role, not just the one named here. Without
    --confirm, only previews what would be dropped. Generic over any
    [databases.*] entry.
    """
    try:
        stack = load_stack_config()
        resolved = Resolver(stack).resolve_database(database)
        engine = resolved.create_engine(role=role)
        try:
            with engine.begin() as connection:
                preview = drop_orphan_schema_tables(
                    connection, stack=stack, orphan_schema=schema, confirm=confirm
                )
        finally:
            engine.dispose()
    except FileNotFoundError:
        err_console.print(f"[red]Config file not found:[/red] {CONFIG_PATH}")
        raise typer.Exit(1)
    except Exception as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)

    if not preview:
        console.print(f"No tables found in schema {schema!r}.")
        return
    for item in preview:
        count = "unknown" if item.row_count is None else str(item.row_count)
        verb = "Dropped" if confirm else "Would drop"
        console.print(f"{verb} {schema}.{item.table_name} (~{count} rows)")
    if not confirm:
        console.print("[yellow]Preview only. Re-run with --confirm to actually drop these tables.[/yellow]")


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

@app.command("cleanup-test-databases")
def cleanup_test_databases(
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Actually drop the selected test databases."),
    ] = False,
    connection: Annotated[
        list[str] | None,
        typer.Option("--connection", help="Test connection to clean; repeatable."),
    ] = None,
) -> None:
    """Preview or drop configured PostgreSQL test databases.

    Only connections marked ``test_only=true`` are eligible. Without
    ``--confirm`` this command only previews the selected databases.
    """
    try:
        from .testing.postgres import PostgresTestStrategy
    except ImportError as exc:
        raise typer.BadParameter(
            "cleanup-test-databases needs the dev extras (pytest). "
            "Install with: pip install 'oa-configurator[dev]'."
        ) from exc

    config = load_stack_config()
    selected = set(connection or config.connections)
    unknown = selected - config.connections.keys()
    if unknown:
        raise typer.BadParameter(f"Unknown connection(s): {', '.join(sorted(unknown))}")

    targets = []
    resolver = Resolver(config)
    for name in sorted(selected):
        entry = config.connections[name]
        if not entry.test_only or not entry.dialect.startswith(Dialect.POSTGRESQL):
            continue
        targets.append((name, resolver.resolve_connection(name)))

    if not targets:
        console.print("[yellow]No test-only PostgreSQL connections selected.[/yellow]")
        return

    console.print("Selected test databases:")
    for name, target in targets:
        console.print(f"  {name}: {target.safe_url}")
    if not confirm:
        console.print("[yellow]Preview only. Re-run with --confirm to drop them.[/yellow]")
        return

    strategy = PostgresTestStrategy()
    for name, target in targets:
        dropped = strategy.drop_test_database(target)
        status = "dropped" if dropped else "already absent"
        console.print(f"{name}: {status}")


@app.command(name="configure", cls=_DynamicConfigureGroup)  # ty: ignore[invalid-argument-type]
def configure(ctx: typer.Context) -> None:
    r"""Configure a package's \[tools.<name>] section.

    Run 'omop-config configure <package> --help' to see that package's flags.
    Packages register support via the 'omop.config' entry-point group.
    """
    if ctx.invoked_subcommand is None:
        _list_packages()
