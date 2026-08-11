"""Resources domain CLI: `connections add/list` and `databases add/list`."""

from __future__ import annotations

from typing import Annotated

import typer

from ...cli_support import _add_entry, _list_entries
from .schema import (
    CDMDatabaseConfig,
    ConnectionConfig,
    DatabaseConfig,
    DatabaseKind,
    GenericDatabaseConfig
)

connections_app = typer.Typer(name="connections", no_args_is_help=True, help=r"Manage \[connections] entries (physical connections).")
databases_app = typer.Typer(name="databases", no_args_is_help=True, help=r"Manage \[databases] entries (generic or CDM/vocab/results bundles).")


@connections_app.command("add")
def connections_add(
    name: Annotated[str, typer.Argument(help="Connection entry name, e.g. 'cdm'.")],
    dialect: Annotated[str | None, typer.Option(help=ConnectionConfig.model_fields["dialect"].description)] = None,
    host: Annotated[str | None, typer.Option(help=ConnectionConfig.model_fields["host"].description)] = None,
    port: Annotated[str | None, typer.Option(help=ConnectionConfig.model_fields["port"].description)] = None,
    user: Annotated[str | None, typer.Option(help=ConnectionConfig.model_fields["user"].description)] = None,
    password: Annotated[str | None, typer.Option(help=ConnectionConfig.model_fields["password"].description)] = None,
    database_name: Annotated[str | None, typer.Option(help=ConnectionConfig.model_fields["database_name"].description)] = None,
    test_only: Annotated[str | None, typer.Option(help=ConnectionConfig.model_fields["test_only"].description)] = None,
) -> None:
    r"""Add or update a \[connections.<name>] entry. Prompts for any field not given as a flag.

    ``--test-only`` accepts true/false/yes/no/1/0.
    """
    flags = {
        k: v for k, v in {
            "dialect": dialect, "host": host, "port": port,
            "user": user, "password": password, "database_name": database_name,
            "test_only": test_only,
        }.items() if v is not None
    } or None
    _add_entry(ConnectionConfig, "connections", name, flags)


@connections_app.command("list")
def connections_list() -> None:
    """List configured connection entries."""
    _list_entries(ConnectionConfig, "connections")


@databases_app.command("add")
def databases_add(
    name: Annotated[str, typer.Argument(help="Database entry name, e.g. 'cdm_db'.")],
    kind: Annotated[
        DatabaseKind | None,
        typer.Option(help=f"Database kind, one of: {', '.join(member.value for member in DatabaseKind)}."),
    ] = None,
    connection: Annotated[str | None, typer.Option(help=DatabaseConfig.model_fields["connection"].description)] = None,
    schema_name: Annotated[str | None, typer.Option(help=DatabaseConfig.model_fields["schema_name"].description)] = None,
    vocab_connection: Annotated[str | None, typer.Option(help=CDMDatabaseConfig.model_fields["vocab_connection"].description)] = None,
    vocab_schema: Annotated[str | None, typer.Option(help=CDMDatabaseConfig.model_fields["vocab_schema"].description)] = None,
    results_schema: Annotated[str | None, typer.Option(help=CDMDatabaseConfig.model_fields["results_schema"].description)] = None,
) -> None:
    r"""Add or update a \[databases.<name>] entry. Prompts for any field not given as a flag.

    ``--kind`` picks which shape this entry has and must be given (or answered when prompted)
    before anything else: it decides which of the other flags even apply. Required whenever any
    other flag is given; prompted for first when adding fully interactively.
    """
    other_flags = {
        k: v for k, v in {
            "connection": connection, "schema_name": schema_name,
            "vocab_connection": vocab_connection, "vocab_schema": vocab_schema,
            "results_schema": results_schema,
        }.items() if v is not None
    }
    kind_choices = [member.value for member in DatabaseKind]
    if kind is None:
        if other_flags:
            from rich.console import Console
            Console(stderr=True).print(
                f"[red bold]--kind is required[/red bold] alongside any other flag ({', '.join(kind_choices)})."
            )
            raise typer.Exit(1)
        import click
        kind = DatabaseKind(
            click.prompt("  kind", type=click.Choice(kind_choices), default=DatabaseKind.GENERIC.value)
        )
    target = CDMDatabaseConfig if kind is DatabaseKind.CDM else GenericDatabaseConfig
    _add_entry(target, "databases", name, other_flags or None)


@databases_app.command("list")
def databases_list() -> None:
    """List configured database entries."""
    _list_entries(DatabaseConfig, "databases")
