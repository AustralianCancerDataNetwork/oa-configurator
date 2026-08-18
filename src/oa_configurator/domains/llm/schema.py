"""LLM domain: provider connections and named, concretely-configured models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...refs import RefTo, Secret

if TYPE_CHECKING:
    from ...stack_config import StackConfig


class ProviderConfig(BaseModel):
    """Concrete connection to one LLM provider: which one, and how to reach it.

    Peer of :class:`~oa_configurator.domains.resources.schema.ConnectionConfig`
    for LLM/embedding backends instead of databases. Referenced by
    :attr:`ModelConfig.provider`. Each entry under ``[providers]`` in
    ``config.toml`` maps to one instance of this model.

    API keys are stored in plaintext for now, the same documented
    limitation ``ConnectionConfig.password`` already carries; secret
    management support is planned for a future release for both.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        description="Provider key, e.g. 'ollama', 'llamacpp', 'vllm', 'openai', 'anthropic', 'gemini'."
    )
    base_url: str | None = Field(
        default=None,
        description="Base URL for this specific deployment of the provider (a local llama-server, a cloud vendor endpoint, and so on).",
    )
    api_key: Secret = Field(
        default=None,
        description="Plaintext API key for this deployment, if one is required. Secret management support is planned for a future release.",
    )

    @field_validator("base_url")
    @classmethod
    def _reject_userinfo(cls, v: str | None) -> str | None:
        """Reject a URL carrying userinfo (``https://user:pw@host/v1``).
        """
        if v is None:
            return v
        try:
            parts = urlsplit(v)
        except ValueError as exc:
            raise ValueError(f"base_url is not a valid URL: {exc}") from None
        if parts.username is not None or parts.password is not None:
            raise ValueError(
                "base_url must not contain userinfo (the 'user:password@' part "
                "before the host). Put the credential in this provider's "
                "`api_key` field, which is declared Sensitive() and is masked "
                "wherever the stack renders configuration."
            )
        return v

    def resolve(self, name: str) -> ResolvedProvider:
        """Resolve this provider to a concrete, backend-ready connection."""
        return ResolvedProvider(name=name, provider=self.provider, base_url=self.base_url, api_key=self.api_key)


class ModelConfig(BaseModel):
    """A named, reusable, concretely-configured model.

    Peer of :class:`~oa_configurator.domains.resources.schema.DatabaseConfig`
    for LLM/embedding backends instead of databases. The unit that consuming
    packages reference by name (e.g. a package's ``embedding_model_name``
    field just names an entry here). Each entry under ``[models]`` in
    ``config.toml`` maps to one instance of this model.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Annotated[str, RefTo(ProviderConfig)] = Field(
        description="Name of the provider entry (from [providers]) this model is served through."
    )
    model: str = Field(
        description="Model name or identifier passed to the provider."
    )
    embedding_dim: int | None = Field(
        default=None,
        description="Embedding dimension override. Unset lets the provider's own discovery (fast path or live probe) determine it.",
    )
    document_prefix: str | None = Field(
        default=None,
        description="Prefix prepended to document/passage text before embedding, for asymmetric embedding models (e.g. nomic-embed-text, E5, BGE).",
    )
    query_prefix: str | None = Field(
        default=None,
        description="Prefix prepended to query text before embedding, for asymmetric embedding models (e.g. nomic-embed-text, E5, BGE).",
    )
    embeddings: bool = Field(
        default=False,
        description="Whether this specific model supports the embeddings endpoint. Opt-in: neither any-llm nor omop-llm can introspect this per model, so it isn't assumed.",
    )
    tool_use: bool = Field(
        default=False,
        description="Whether this specific model supports tool/function calling. Opt-in, see `embeddings`.",
    )
    structured_output: bool = Field(
        default=False,
        description="Whether this specific model supports structured (schema-constrained) output. Opt-in, see `embeddings`.",
    )
    extended_thinking: bool = Field(
        default=False,
        description="Whether this specific model supports reasoning/extended-thinking output. Opt-in, see `embeddings`.",
    )
    configuration: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form per-model knobs (max_tokens, temperature, and so on) with no dedicated field, passed through verbatim to the underlying call.",
    )

    @model_validator(mode="after")
    def validate_embedding_configuration(self) -> ModelConfig:
        """Reject embedding dimensions on models without embedding support."""
        if self.embedding_dim is not None and not self.embeddings:
            raise ValueError("embedding_dim requires embeddings=true")
        return self

    def resolve(self, name: str, stack: StackConfig) -> ResolvedModel:
        """Resolve this model to a concrete, backend-ready configuration.

        *stack* must already have passed :meth:`StackConfig.validate_references`,
        so ``self.provider`` is guaranteed to exist in ``stack.providers``.
        """
        provider = stack.providers[self.provider].resolve(self.provider)
        return ResolvedModel(
            name=name,
            provider=provider,
            model=self.model,
            embedding_dim=self.embedding_dim,
            document_prefix=self.document_prefix,
            query_prefix=self.query_prefix,
            embeddings=self.embeddings,
            tool_use=self.tool_use,
            structured_output=self.structured_output,
            extended_thinking=self.extended_thinking,
            configuration=dict(self.configuration),
        )


@dataclass(frozen=True)
class ResolvedProvider:
    """Concrete LLM provider connection, ready to be served through.

    Attributes
    ----------
    name : str
        Logical name of the provider as declared in the config.
    provider : str
        Provider key, e.g. ``'ollama'``, ``'llamacpp'``, ``'openai'``.
    base_url : str, optional
        Resolved base URL for this deployment.
    api_key : str, optional
        Resolved API key for this deployment.
    """

    name: str
    provider: str
    base_url: str | None
    api_key: str | None

    def __repr__(self) -> str:
        return f"ResolvedProvider(name={self.name!r}, provider={self.provider!r})"


@dataclass(frozen=True)
class ResolvedModel:
    """Concrete, backend-agnostic model configuration.
    No explicit methods as it is just a data struct that can be used
    by the consuming package to construct a backend-specific model handle.

    Attributes
    ----------
    name : str
        Logical name of the model as declared in the config.
    provider : ResolvedProvider
        Resolved provider this model is served through.
    model : str
        Model name or identifier passed to the provider.
    embedding_dim : int, optional
        Embedding dimension override, or None to let the provider's own
        discovery determine it.
    document_prefix : str, optional
        Prefix prepended to document/passage text before embedding, for
        asymmetric embedding models.
    query_prefix : str, optional
        Prefix prepended to query text before embedding, for asymmetric
        embedding models.
    embeddings : bool
        Whether this specific model supports the embeddings endpoint.
    tool_use : bool
        Whether this specific model supports tool/function calling.
    structured_output : bool
        Whether this specific model supports structured (schema-constrained)
        output.
    extended_thinking : bool
        Whether this specific model supports reasoning/extended-thinking
        output.
    configuration : dict[str, Any]
        Free-form per-model knobs (max_tokens, temperature, and so on) with
        no dedicated field.
    """

    name: str
    provider: ResolvedProvider
    model: str
    embedding_dim: int | None
    document_prefix: str | None
    query_prefix: str | None
    embeddings: bool
    tool_use: bool
    structured_output: bool
    extended_thinking: bool
    configuration: dict[str, Any]

    def __repr__(self) -> str:
        return (
            f"ResolvedModel(name={self.name!r}, "
            f"provider={self.provider.provider!r}, model={self.model!r})"
        )
