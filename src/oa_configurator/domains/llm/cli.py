"""LLM domain CLI: `providers add/list` and `models add/list`."""

from __future__ import annotations

from typing import Annotated

import typer

from ...cli_support import _add_entry, _list_entries
from .schema import ModelConfig, ProviderConfig

providers_app = typer.Typer(name="providers", no_args_is_help=True, help=r"Manage \[providers] entries (LLM/embedding provider connections).")
models_app = typer.Typer(name="models", no_args_is_help=True, help=r"Manage \[models] entries (named, concretely-configured models).")


@providers_app.command("add")
def providers_add(
    name: Annotated[str, typer.Argument(help="Provider entry name, e.g. 'ollama-local'.")],
    provider: Annotated[str | None, typer.Option(help=ProviderConfig.model_fields["provider"].description)] = None,
    base_url: Annotated[str | None, typer.Option(help=ProviderConfig.model_fields["base_url"].description)] = None,
    api_key: Annotated[str | None, typer.Option(help=ProviderConfig.model_fields["api_key"].description)] = None,
) -> None:
    r"""Add or update a \[providers.<name>] entry. Prompts for any field not given as a flag."""
    flags = {
        k: v for k, v in {"provider": provider, "base_url": base_url, "api_key": api_key}.items()
        if v is not None
    } or None
    _add_entry(ProviderConfig, "providers", name, flags)


@providers_app.command("list")
def providers_list() -> None:
    """List configured provider entries."""
    _list_entries(ProviderConfig, "providers")


@models_app.command("add")
def models_add(
    name: Annotated[str, typer.Argument(help="Model entry name, e.g. 'nomic-embed'.")],
    provider: Annotated[str | None, typer.Option(help=ModelConfig.model_fields["provider"].description)] = None,
    model: Annotated[str | None, typer.Option(help=ModelConfig.model_fields["model"].description)] = None,
    embedding_dim: Annotated[str | None, typer.Option(help=ModelConfig.model_fields["embedding_dim"].description)] = None,
    document_prefix: Annotated[str | None, typer.Option(help=ModelConfig.model_fields["document_prefix"].description)] = None,
    query_prefix: Annotated[str | None, typer.Option(help=ModelConfig.model_fields["query_prefix"].description)] = None,
) -> None:
    r"""Add or update a \[models.<name>] entry. Prompts for any field not given as a flag."""
    flags = {
        k: v for k, v in {
            "provider": provider, "model": model, "embedding_dim": embedding_dim,
            "document_prefix": document_prefix, "query_prefix": query_prefix,
        }.items() if v is not None
    } or None
    _add_entry(ModelConfig, "models", name, flags)


@models_app.command("list")
def models_list() -> None:
    """List configured model entries."""
    _list_entries(ModelConfig, "models")
