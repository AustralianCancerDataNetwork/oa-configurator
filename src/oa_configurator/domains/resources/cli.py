"""Resources domain CLI: `connections add/list` and `databases add/list`."""

from __future__ import annotations

from typing import Annotated

import typer

from ...cli_support import _add_entry, _list_entries
from .schema import (
    CDMDatabaseConfig,
    ConnectionConfig,
    DatabaseConfig,
    GenericDatabaseConfig
)

connections_app = typer.Typer(name="connections", no_args_is_help=True, help=r"Manage \[connections] entries (physical connections).")
databases_app = typer.Typer(name="databases", no_args_is_help=True, help=r"Manage \[databases] entries (generic or CDM/vocab/results bundles).")
databases_add_app = typer.Typer(
    name="add", no_args_is_help=True,
    help=r"Add or update a \[databases.<name>] entry. Pick the subcommand matching the kind you want.",
)
databases_app.add_typer(databases_add_app)


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


@databases_add_app.command("generic")
def databases_add_generic(
    name: Annotated[str, typer.Argument(help="Database entry name, e.g. 'emb_db'.")],
    connection: Annotated[str | None, typer.Option(help=DatabaseConfig.model_fields["connection"].description)] = None,
    schema_name: Annotated[str | None, typer.Option(help=GenericDatabaseConfig.model_fields["schema_name"].description)] = None,
) -> None:
    r"""Add or update a generic \[databases.<name>] entry. Prompts for any field not given as a flag."""
    other_flags = {
        k: v for k, v in {"connection": connection, "schema_name": schema_name}.items()
        if v is not None
    }
    _add_entry(GenericDatabaseConfig, "databases", name, other_flags or None)


@databases_add_app.command("cdm")
def databases_add_cdm(
    name: Annotated[str, typer.Argument(help="Database entry name, e.g. 'cdm_db'.")],
    connection: Annotated[str | None, typer.Option(help=DatabaseConfig.model_fields["connection"].description)] = None,
    cdm_schema: Annotated[str | None, typer.Option(help=CDMDatabaseConfig.model_fields["cdm_schema"].description)] = None,
    vocab_connection: Annotated[str | None, typer.Option(help=CDMDatabaseConfig.model_fields["vocab_connection"].description)] = None,
    vocab_schema: Annotated[str | None, typer.Option(help=CDMDatabaseConfig.model_fields["vocab_schema"].description)] = None,
    results_schema: Annotated[str | None, typer.Option(help=CDMDatabaseConfig.model_fields["results_schema"].description)] = None,
) -> None:
    r"""Add or update a CDM \[databases.<name>] entry. Prompts for any field not given as a flag."""
    other_flags = {
        k: v for k, v in {
            "connection": connection, "cdm_schema": cdm_schema,
            "vocab_connection": vocab_connection, "vocab_schema": vocab_schema,
            "results_schema": results_schema,
        }.items() if v is not None
    }
    _add_entry(CDMDatabaseConfig, "databases", name, other_flags or None)


@databases_app.command("list")
def databases_list() -> None:
    """List configured database entries."""
    _list_entries(DatabaseConfig, "databases")
