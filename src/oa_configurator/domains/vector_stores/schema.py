"""Vector-stores domain: which storage backend an embedding subsystem uses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from ...refs import RefTo
from ..resources.schema import DatabaseConfig, ResolvedDatabase

if TYPE_CHECKING:
    from ...stack_config import StackConfig


class VectorStoreConfig(BaseModel):
    """Which storage backend an embedding-capable package should use.

    Peer of :class:`~oa_configurator.domains.resources.schema.DatabaseConfig`
    and :class:`~oa_configurator.domains.llm.schema.ModelConfig`: the unit
    that consuming packages reference by name (e.g. a package's
    ``vector_store_name`` field just names an entry here). Each entry under
    ``[vector_stores]`` in ``config.toml`` maps to one instance of this model.

    ``backend_type`` is deliberately a plain string, not an enum imported
    from the owning package (e.g. omop-emb's ``BackendType``) -- oa-configurator
    never needs to know which backend keys are valid, the same way
    :attr:`~oa_configurator.domains.llm.schema.ProviderConfig.provider` never
    needs to know which LLM providers any-llm supports. Validation of the
    value happens in the owning package's own resolution code.
    """

    model_config = ConfigDict(extra="forbid")

    backend_type: str = Field(
        description="Storage backend key, e.g. 'sqlitevec', 'pgvector'. Validated by the owning package, not here."
    )
    database: Annotated[str | None, RefTo(DatabaseConfig)] = Field(
        default=None,
        description="Name of the database entry (from [databases]) for a database-backed store, e.g. pgvector.",
    )
    sqlite_path: str | None = Field(
        default=None,
        description="Path to a local file-backed store, e.g. sqlite-vec.",
    )
    configuration: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form per-store knobs (e.g. a FAISS cache directory) with no dedicated field, passed through verbatim.",
    )

    def resolve(self, name: str, stack: StackConfig) -> ResolvedVectorStore:
        """Resolve this vector store to a concrete, backend-ready configuration.

        *stack* must already have passed :meth:`StackConfig.validate_references`,
        so ``self.database`` is guaranteed to exist in ``stack.databases`` when set.
        """
        resolved_database = (
            stack.databases[self.database].resolve(self.database, stack) if self.database else None
        )
        return ResolvedVectorStore(
            name=name,
            backend_type=self.backend_type,
            database=resolved_database,
            sqlite_path=self.sqlite_path,
            configuration=dict(self.configuration),
        )


@dataclass(frozen=True)
class ResolvedVectorStore:
    """Concrete, backend-agnostic vector-store configuration.

    No explicit methods; a plain data struct the consuming package uses to
    construct its own backend-specific handle.

    Attributes
    ----------
    name : str
        Logical name of the vector store as declared in the config.
    backend_type : str
        Storage backend key, e.g. ``'sqlitevec'``, ``'pgvector'``.
    database : ResolvedDatabase, optional
        Resolved database for a database-backed store, or ``None``.
    sqlite_path : str, optional
        Path to a local file-backed store, or ``None``.
    configuration : dict[str, Any]
        Free-form per-store knobs with no dedicated field.
    """

    name: str
    backend_type: str
    database: ResolvedDatabase | None
    sqlite_path: str | None
    configuration: dict[str, Any]

    def __repr__(self) -> str:
        return f"ResolvedVectorStore(name={self.name!r}, backend_type={self.backend_type!r})"
