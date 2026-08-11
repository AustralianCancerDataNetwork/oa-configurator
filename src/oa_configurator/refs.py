"""Generic field markers shared across every domain schema.

Pure, dependency-free (beyond pydantic): no schema class, StackConfig, or
domain lives here, so every domain module and the root StackConfig can both
import from here without a cycle.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class RefTo:
    """Marks a string field as naming an entry in another top-level section.

    Applied via e.g. ``Annotated[str, RefTo(ConnectionConfig)]``. One generic
    marker drives both cross-reference validation (:func:`~oa_configurator.stack_config.unresolved_refs`)
    and the CLI wizard's reuse-or-create recursion for a consuming package's
    own fields (e.g. ``embedding_model_name: Annotated[str, RefTo(ModelConfig)]``).
    It replaces a hand-written validator per pair and the separate
    ``ModelFieldSpec``/``referenced_models`` side-list that used to carry the
    same information for consumer fields.

    Attributes
    ----------
    target : type[BaseModel]
        The section this field's value should name an entry in.
    is_test : bool
        Whether this field is expected to resolve to a ``test_only``
        connection (for ``target=DatabaseConfig`` fields). Replaces a
        previous, unreliable convention of inferring this from whether the
        field's own Python name started with ``"test_"``. Drives both the
        CLI wizard's "Test database (optional)" prompt and the symmetric
        ``is_test``/``test_only`` match enforced by
        :meth:`~oa_configurator.resolver.Resolver.resolve_package_config`.
    """

    target: type[BaseModel]
    is_test: bool = False


@dataclass(frozen=True)
class Sensitive:
    """Marks a string field as holding a secret: masked when interactively
    prompted, and a future anchor for ``secret_source`` (``env:``/``file:``)
    resolution. Applied via e.g. ``Annotated[str | None, Sensitive()]``.
    """


def _iter_refs(cls: type[BaseModel]) -> Iterator[tuple[str, RefTo]]:
    """Yield (field_name, RefTo) for every RefTo-marked field on *cls*.

    At most one RefTo per field is meaningful, since a field names an entry
    in exactly one section, so the first one found wins. A field carrying
    more than one is a mistake, not a supported case.
    """
    for name, info in cls.model_fields.items():
        refs = [m for m in info.metadata if isinstance(m, RefTo)]
        assert len(refs) <= 1, f"{cls.__name__}.{name} has more than one RefTo marker"
        if refs:
            yield name, refs[0]


def is_sensitive(info: Any) -> bool:
    """Whether a field is marked `Sensitive` and should be masked when prompted."""
    return any(isinstance(m, Sensitive) for m in info.metadata)
