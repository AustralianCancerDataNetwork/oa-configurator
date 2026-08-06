"""Vector-stores domain CLI: `vector-stores add/list`."""

from __future__ import annotations

from typing import Annotated

import typer

from ...cli_support import _add_entry, _list_entries
from .schema import VectorStoreConfig

vector_stores_app = typer.Typer(
    name="vector-stores", no_args_is_help=True, help=r"Manage \[vector_stores] entries (embedding storage backends)."
)


@vector_stores_app.command("add")
def vector_stores_add(
    name: Annotated[str, typer.Argument(help="Vector store entry name, e.g. 'vector_store'.")],
    backend_type: Annotated[str | None, typer.Option(help=VectorStoreConfig.model_fields["backend_type"].description)] = None,
    database: Annotated[str | None, typer.Option(help=VectorStoreConfig.model_fields["database"].description)] = None,
) -> None:
    r"""Add or update a \[vector_stores.<name>] entry. Prompts for any field not given as a flag."""
    flags = {
        k: v for k, v in {
            "backend_type": backend_type, "database": database,
        }.items() if v is not None
    } or None
    _add_entry(VectorStoreConfig, "vector_stores", name, flags)


@vector_stores_app.command("list")
def vector_stores_list() -> None:
    """List configured vector store entries."""
    _list_entries(VectorStoreConfig, "vector_stores")
