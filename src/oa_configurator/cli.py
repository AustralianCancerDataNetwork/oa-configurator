"""Small CLI surface for inspecting and resolving stack configuration."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Callable, Literal, TypeVar, cast
import tomllib

import typer
import sqlalchemy as sa
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.pretty import Pretty
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from .loader import load_stack_config
from .models import ConnectionConfig, ProfileConfig, ResourceConfig, SettingsConfig, StackConfig
from .persistence import save_stack_config
from .resolver import ResolvedDatabaseTarget, Resolver
from .settings import DEFAULT_CONFIG_PATH

app = typer.Typer(help="CLI for inspecting and editing oa_configurator settings.")
console = Console()
ChoiceT = TypeVar("ChoiceT", bound=str)


@app.command("show")
def show_config(path: Path | None = typer.Option(None, help="Path to config.toml.")) -> None:
    """Print the validated configuration as formatted JSON."""

    config = load_stack_config(path)
    _title("Configuration")
    console.print(JSON.from_data(config.model_dump(mode="json")))


@app.command("resolve-resource")
def resolve_resource(
    name: str,
    path: Path | None = typer.Option(None, help="Path to config.toml."),
) -> None:
    """Resolve and print one logical resource bundle."""

    config = load_stack_config(path)
    resolved = Resolver(config).resolve_resource(name)
    _title(f"Resolved Resource: {name}")
    console.print(Panel.fit(Pretty(resolved), border_style="cyan"))


@app.command("resolve-tool")
def resolve_tool(
    name: str,
    path: Path | None = typer.Option(None, help="Path to config.toml."),
) -> None:
    """Resolve and print one tool-default entry."""

    config = load_stack_config(path)
    resolved = Resolver(config).resolve_tool(name)
    _title(f"Resolved Tool: {name}")
    console.print(Panel.fit(Pretty(resolved), border_style="cyan"))


@app.command("add-profile")
def add_profile(
    name: str | None = typer.Option(None, help="Name of the profile to create."),
    path: Path | None = typer.Option(None, help="Path to config.toml."),
) -> None:
    """Walk through the setup of a new profile."""

    config, resolved_path = _load_or_create_config(path)
    _title("New Profile")
    _note(f"Config file: {resolved_path}")

    profile_name = name or _prompt_text("Profile name")
    if profile_name in config.profiles:
        raise typer.BadParameter(f"Profile {profile_name!r} already exists.")

    suggested_description = _suggest_profile_description(profile_name)
    description = _optional_prompt("Description", default=suggested_description or "")
    config.profiles[profile_name] = ProfileConfig(description=description)

    if _confirm("Make this the active profile?", default=False):
        config.settings.active_profile = profile_name

    save_stack_config(config, resolved_path)
    _success(f"Saved profile {profile_name!r} to {resolved_path}")


@app.command("add-connection")
def add_connection(
    name: str | None = typer.Option(None, help="Name of the connection to create."),
    path: Path | None = typer.Option(None, help="Path to config.toml."),
) -> None:
    """Walk through the setup of a new connection."""

    config, resolved_path = _load_or_create_config(path)
    _title("New Connection")
    _note(f"Config file: {resolved_path}")

    connection_name = name or _prompt_text("Connection name")
    if connection_name in config.connections:
        raise typer.BadParameter(f"Connection {connection_name!r} already exists.")

    inferred_kind: Literal["database", "file"] = (
        "file" if any(token in connection_name.lower() for token in ("sqlite", "file", "duckdb")) else "database"
    )
    kind = _prompt_choice(
        "Connection kind",
        ("database", "file"),
        default=inferred_kind,
    )
    defaults = _suggest_connection_defaults(connection_name, kind)
    dialect = _prompt_text("Dialect", default=str(defaults["dialect"]))

    if kind == "file":
        file_path = _prompt_text("Path to the database/file", default=str(defaults["path"]))
        connection = ConnectionConfig(
            kind=kind,
            dialect=dialect,
            path=file_path,
            read_only=_confirm("Read only?", default=False),
        )
    elif dialect == "sqlite":
        file_path = _prompt_text("Path to the database/file", default=str(defaults["path"]))
        database = None
        if dialect == "sqlite" and _confirm("Store sqlite file path in the 'database' field instead?", default=False):
            database = file_path
            file_path = None
        connection = ConnectionConfig(
            kind=kind,
            dialect=dialect,
            path=file_path,
            database=database,
            read_only=_confirm("Read only?", default=False),
        )
    else:
        host = _prompt_text("Host", default=str(defaults["host"]))
        port = _prompt_int("Port", default=int(defaults["port"]))
        user = _prompt_text("User", default=str(defaults["user"]))
        password = _prompt_text("Password", default="", password=True)
        secret_source = None
        if password == "":
            secret_source = _optional_prompt(
                "Secret source (env:NAME or file:PATH)",
                default="",
            )
        database = _prompt_text("Database name", default=str(defaults["database"]))
        connection = ConnectionConfig(
            kind=kind,
            dialect=dialect,
            host=host,
            port=port,
            user=user or None,
            password=password or None,
            secret_source=secret_source,
            database=database,
            read_only=_confirm("Read only?", default=False),
        )

    resolved_connection = _resolve_preview_connection(
        connection_name,
        connection,
        config,
    )
    _connection_preview(resolved_connection)

    if _confirm("Test this connection now?", default=True):
        success, message = _test_connection(resolved_connection)
        if success:
            _success(f"Connection test succeeded: {message}")
        else:
            _error(f"Connection test failed: {message}")
            if not _confirm("Save this connection anyway?", default=False):
                _note("Connection was not saved.")
                raise typer.Exit(code=1)

    config.connections[connection_name] = connection
    save_stack_config(config, resolved_path)
    _success(f"Saved connection {connection_name!r} to {resolved_path}")


@app.command("add-resource")
def add_resource(
    name: str | None = typer.Option(None, help="Name of the resource to create."),
    path: Path | None = typer.Option(None, help="Path to config.toml."),
) -> None:
    """Walk through the setup of a new logical resource."""

    config, resolved_path = _load_or_create_config(path)
    _title("New Resource")
    _note(f"Config file: {resolved_path}")

    resource_name = name or _prompt_text("Resource name")
    if resource_name in config.resources:
        raise typer.BadParameter(f"Resource {resource_name!r} already exists.")
    if not config.connections:
        raise typer.BadParameter("Create at least one connection before creating a resource.")

    resolver = Resolver(config)
    _show_names_table("Known connections", resolver.connection_names())

    primary_default = _suggest_primary_connection_default(resolver.connection_names(), resource_name)
    primary_db = _prompt_existing_name("Primary DB connection", resolver.connection_names, default=primary_default)

    if _confirm("Use the same connection for vocabulary data?", default=True):
        vocab_db = primary_db
    else:
        vocab_db = _prompt_existing_name("Vocab DB connection", resolver.connection_names, default=primary_db)

    if _confirm("Use the same connection for results data?", default=True):
        results_db = primary_db
    else:
        results_db = _prompt_existing_name("Results DB connection", resolver.connection_names, default=primary_db)

    resource_defaults = _suggest_resource_defaults(resource_name, primary_db)

    omop_schema = _optional_prompt("OMOP schema", default=resource_defaults["omop_schema"])
    vocab_schema = _optional_prompt("Vocab schema", default=resource_defaults["vocab_schema"] or omop_schema or "")
    results_schema = _optional_prompt("Results schema", default=resource_defaults["results_schema"])

    _note(
        "Filesystem paths may be absolute or relative to settings.configuration_base_path "
        f"({config.settings.configuration_base_path!r})."
    )
    athena_source_path = _optional_prompt("Athena source path", default=resource_defaults["athena_source_path"])
    artifact_root = _optional_prompt("Artifact root", default=resource_defaults["artifact_root"])
    embedding_file_root = _optional_prompt("Embedding file root", default=resource_defaults["embedding_file_root"])
    analytic_db_file_root = _optional_prompt("Analytic DB file root", default=resource_defaults["analytic_db_file_root"])

    config.resources[resource_name] = ResourceConfig(
        primary_db=primary_db,
        vocab_db=vocab_db,
        results_db=results_db,
        omop_schema=omop_schema,
        vocab_schema=vocab_schema,
        results_schema=results_schema,
        athena_source_path=athena_source_path,
        artifact_root=artifact_root,
        embedding_file_root=embedding_file_root,
        analytic_db_file_root=analytic_db_file_root,
    )

    save_stack_config(config, resolved_path)
    _success(f"Saved resource {resource_name!r} to {resolved_path}")


def _load_or_create_config(path: Path | None) -> tuple[StackConfig, Path]:
    """Load an existing config or create a minimal new in-memory one."""

    resolved_path = Path(path).expanduser() if path is not None else DEFAULT_CONFIG_PATH
    if resolved_path.exists():
        return load_stack_config(resolved_path), resolved_path

    config = StackConfig(settings=SettingsConfig())
    config.bind_loaded_path(resolved_path)
    return config, resolved_path


def _resolve_preview_connection(
    name: str,
    connection: ConnectionConfig,
    config: StackConfig,
) -> ResolvedDatabaseTarget:
    """Resolve an unsaved connection against the current config context."""

    preview_config = config.model_copy(deep=True)
    preview_config.connections[name] = connection
    return Resolver(preview_config).resolve_connection(name)


def _optional_prompt(label: str, *, default: str = "") -> str | None:
    """Prompt for an optional string field and normalize blank input to ``None``."""

    value = _prompt_text(label, default=default).strip()
    return value or None


def _prompt_choice(label: str, choices: tuple[ChoiceT, ...], *, default: ChoiceT | None = None) -> ChoiceT:
    """Prompt until one of the allowed literal choices is selected."""

    choice_text = ", ".join(choices)
    while True:
        value = _prompt_text(f"{label} [{choice_text}]", default=default or choices[0]).strip()
        if value in choices:
            return cast(ChoiceT, value)
        _error(f"Please choose one of: {choice_text}")


def _prompt_existing_name(
    label: str,
    names_supplier: Callable[[], tuple[str, ...]],
    *,
    default: str | None = None,
) -> str:
    """Prompt until the user selects one of the known configured names."""

    choices = names_supplier()
    while True:
        prompt_default = default if default is not None else (choices[0] if len(choices) == 1 else None)
        value = _prompt_text(label, default=prompt_default).strip()
        if value in choices:
            return value
        _error(f"Unknown name {value!r}. Known values: {', '.join(choices)}")


def _suggest_profile_description(name: str) -> str | None:
    """Return a lightweight suggested description from a profile name."""

    lowered = name.lower()
    if lowered == "local":
        return "everything local"
    if lowered == "prod":
        return "remote OMOP source with local derived artifacts"
    if lowered == "staging":
        return "staging environment for pre-production validation"
    if lowered in {"dev", "development"}:
        return "development environment"
    if lowered == "ci":
        return "continuous integration environment"
    if lowered == "test":
        return "test environment"
    return None


def _suggest_connection_defaults(name: str, kind: str) -> dict[str, str | int]:
    """Suggest connection defaults from the connection name and example config."""

    lowered = name.lower()
    example = _load_example_defaults()

    if kind == "file":
        if "duckdb" in lowered:
            return {
                "dialect": "duckdb",
                "path": f"data/{name}.duckdb",
            }
        return {
            "dialect": "sqlite",
            "path": f"data/{name}.sqlite",
        }

    if "local" in lowered:
        return {
            "dialect": "postgresql",
            "host": "localhost",
            "port": 5432,
            "user": "omop",
            "database": "omop",
        }

    if "prod" in lowered:
        return {
            "dialect": "postgresql",
            "host": "prod.hospital.org",
            "port": 5432,
            "user": "omop_prod",
            "database": "omop_cdm",
        }

    if "staging" in lowered:
        return {
            "dialect": "postgresql",
            "host": "staging.hospital.org",
            "port": 5432,
            "user": "omop_staging",
            "database": "omop",
        }

    return {
        "dialect": str(example.get("connection_dialect", "postgresql")),
        "host": str(example.get("connection_host", "localhost")),
        "port": int(example.get("connection_port", 5432)),
        "user": str(example.get("connection_user", "")),
        "database": str(example.get("connection_database", "omop")),
    }


def _test_connection(connection: ResolvedDatabaseTarget) -> tuple[bool, str]:
    """Attempt to connect using SQLAlchemy and return a friendly result tuple.

    The function is intentionally defensive:

    - it catches driver/import problems
    - it keeps error reporting short
    - it does not raise on ordinary connectivity failures
    """

    url = connection.url
    try:
        engine = sa.create_engine(url, future=True)
    except ModuleNotFoundError as exc:
        return (
            False,
            f"missing database driver dependency ({exc.name}) for dialect {connection.dialect!r}",
        )
    except Exception as exc:
        return False, f"could not build engine for {connection.dialect!r}: {_short_error(exc)}"

    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return True, connection.safe_url
    except Exception as exc:
        return False, _short_error(exc)
    finally:
        engine.dispose()


def _short_error(exc: Exception) -> str:
    """Convert an exception into a short user-facing message."""

    text = str(exc).strip()
    return text or exc.__class__.__name__


def _title(text: str) -> None:
    """Render a short section heading."""

    console.print(f"[bold cyan]{text}[/bold cyan]")


def _success(text: str) -> None:
    """Render a success message with a tick marker."""

    console.print(f"[bold green]✓[/bold green] {text}")


def _error(text: str) -> None:
    """Render an error message with a cross marker."""

    console.print(f"[bold red]✗[/bold red] {text}")


def _note(text: str) -> None:
    """Render a neutral informational line."""

    console.print(f"[bold blue]•[/bold blue] {text}")


def _prompt_text(label: str, default: str | None = None, *, password: bool = False) -> str:
    """Prompt for a text value using Rich."""

    prompt_label = f"[bold cyan]?[/bold cyan] {label}"
    value = Prompt.ask(prompt_label, default=default, password=password)
    if value is None:
        raise RuntimeError(f"Prompt did not return a value for {label!r}")
    return value


def _prompt_int(label: str, default: int) -> int:
    """Prompt for an integer value using Rich."""

    prompt_label = f"[bold cyan]?[/bold cyan] {label}"
    return IntPrompt.ask(prompt_label, default=default)


def _confirm(label: str, *, default: bool) -> bool:
    """Prompt for a yes/no confirmation using Rich."""

    prompt_label = f"[bold cyan]?[/bold cyan] {label}"
    return Confirm.ask(prompt_label, default=default)


def _show_names_table(title: str, names: tuple[str, ...]) -> None:
    """Render a compact single-column table of known names."""

    table = Table(title=title, show_header=True, header_style="bold magenta")
    table.add_column("Name", style="white")
    for name in names:
        table.add_row(name)
    console.print(table)


def _connection_preview(connection: ResolvedDatabaseTarget) -> None:
    """Render a compact preview of the connection before test/save."""

    table = Table(title=f"Connection Preview: {connection.name}", show_header=False, box=None)
    table.add_column("Field", style="bold")
    table.add_column("Value", style="white")
    table.add_row("kind", connection.connection.kind)
    table.add_row("dialect", connection.dialect)
    table.add_row("url", connection.safe_url)
    if connection.connection.secret_source is not None:
        table.add_row("secret_source", connection.connection.secret_source)
    table.add_row("read_only", str(connection.connection.read_only))
    console.print(Panel.fit(table, border_style="cyan"))


def _suggest_primary_connection_default(connection_names: tuple[str, ...], resource_name: str) -> str | None:
    """Pick a sensible default primary connection if one stands out."""

    if len(connection_names) == 1:
        return connection_names[0]

    lowered = resource_name.lower()
    if "local" in lowered:
        for name in connection_names:
            if "local" in name.lower():
                return name
    if "prod" in lowered:
        for name in connection_names:
            if "prod" in name.lower():
                return name
    return None


def _suggest_resource_defaults(resource_name: str, primary_db: str) -> dict[str, str]:
    """Suggest resource defaults from the example config and selected connection."""

    example = _load_example_defaults()
    lowered = resource_name.lower()
    primary_lowered = primary_db.lower()

    if "local" in lowered or "local" in primary_lowered:
        artifact_root = f"artifacts/{resource_name}" if resource_name != "local_all_in_one" else "artifacts/local"
        return {
            "omop_schema": "cdm",
            "vocab_schema": "cdm",
            "results_schema": "results",
            "athena_source_path": "",
            "artifact_root": artifact_root,
            "embedding_file_root": f"{artifact_root}/embeddings",
            "analytic_db_file_root": f"{artifact_root}/databases",
        }

    artifact_root = "artifacts" if resource_name == "default" else f"artifacts/{resource_name}"
    return {
        "omop_schema": str(example.get("resource_omop_schema", "cdm")),
        "vocab_schema": str(example.get("resource_vocab_schema", "vocab")),
        "results_schema": str(example.get("resource_results_schema", "results")),
        "athena_source_path": str(example.get("resource_athena_source_path", "")),
        "artifact_root": artifact_root,
        "embedding_file_root": f"{artifact_root}/embeddings",
        "analytic_db_file_root": f"{artifact_root}/databases",
    }


@cache
def _load_example_defaults() -> dict[str, str | int]:
    """Read a few defaults from the packaged example configuration.

    The goal is not to fully interpret the example file, only to reuse a few
    stable, human-curated defaults when they exist.
    """

    example_path = Path(__file__).resolve().parents[2] / "examples" / "config.toml"
    if not example_path.exists():
        return {}

    data = tomllib.loads(example_path.read_text(encoding="utf-8"))
    defaults: dict[str, str | int] = {}

    local_connection = data.get("connections", {}).get("local_cdm", {})
    defaults["connection_dialect"] = local_connection.get("dialect", "postgresql")
    defaults["connection_host"] = local_connection.get("host", "localhost")
    defaults["connection_port"] = local_connection.get("port", 5432)
    defaults["connection_user"] = local_connection.get("user", "")
    defaults["connection_database"] = local_connection.get("database", "omop")

    default_resource = data.get("resources", {}).get("default", {})
    defaults["resource_omop_schema"] = default_resource.get("omop_schema", "cdm")
    defaults["resource_vocab_schema"] = default_resource.get("vocab_schema", "vocab")
    defaults["resource_results_schema"] = default_resource.get("results_schema", "results")
    defaults["resource_athena_source_path"] = default_resource.get("athena_source_path", "")

    return defaults
