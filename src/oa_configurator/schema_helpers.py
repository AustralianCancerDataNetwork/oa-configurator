"""Helpers for turning resolved resources into SQLAlchemy schema options."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .resolver import ResolvedResource


def schema_translate_map(resource: "ResolvedResource") -> dict[str | None, str | None]:
    """Return a SQLAlchemy schema translate map for one resolved resource.

    Conventions:

    - ``None`` maps to the primary OMOP schema
    - ``"vocab"`` maps to the vocabulary schema when configured
    - ``"results"`` maps to the results schema when configured
    """

    translate_map: dict[str | None, str | None] = {}
    if resource.omop_schema is not None:
        translate_map[None] = resource.omop_schema
    if resource.vocab_schema is not None:
        translate_map["vocab"] = resource.vocab_schema
    if resource.results_schema is not None:
        translate_map["results"] = resource.results_schema
    return translate_map
