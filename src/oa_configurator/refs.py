from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails


@dataclass(frozen=True)
class RefTo:
    """Marks a string field as naming an entry in another top-level section.

    Applied via e.g. ``Annotated[str, RefTo(ConnectionConfig)]``. One generic
    marker drives both cross-reference validation (:func:`~oa_configurator.stack_config.unresolved_refs`)
    and the CLI wizard's reuse-or-create recursion for a consuming package's
    own fields (e.g. ``embedding_model_name: Annotated[str, RefTo(ModelConfig)]``).

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
    """
    Marks a string field as holding a secret: masked when interactively
    prompted, excluded from anything this stack renders for display, and a
    future anchor for ``secret_source`` (``env:``/``file:``) resolution.
    """


Secret = Annotated[str | None, Sensitive()]
"""Shorthand for an optional secret string field: ``api_key: Secret = None``.

The whole design rests on implementers declaring their secrets.
Prefer this over the equivalent  ``Annotated[str | None, Sensitive()]``
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
    """Whether a field carries the :class:`Sensitive` marker.

    The stack's only runtime sensitivity predicate: masking a field in anything,
    rendering configuration, and :class:`SecretSafeBaseModel`'s repr all consult this.

    What it cannot reach is free text, which has no field to look up. 
    
    :class:`~oa_configurator.logging_config.RedactingFormatter` is scoped to URLs
    written by other libraries rather than trying to guess.

    Parameters
    ----------
    info : pydantic.fields.FieldInfo
        A field from ``SomeModel.model_fields``.

    Returns
    -------
    bool
        Whether the field is declared sensitive.
    """
    return any(isinstance(m, Sensitive) for m in info.metadata)


MASK = "***"
"""Rendered in place of a secret value. Shared so displays match each other."""


class SecretSafeBaseModel(BaseModel):
    """Base for config models: ``Sensitive()`` fields are masked in repr and str.

    All config base classes must subclass this base, so that a ``PackageConfigBase``
    subclass declaring its own ``Secret`` field inherits safe rendering automatically.
    """

    def __repr_args__(self) -> Any:
        fields = type(self).model_fields
        for name, value in super().__repr_args__():
            info = fields.get(name) if name is not None else None
            if info is not None and value is not None and is_sensitive(info):
                yield name, MASK
            else:
                yield name, value

    def masked_json(self, *, exclude_none: bool = True, indent: int = 2) -> str:
        """Serialize to JSON for display, with every secret replaced by ``MASK``.

        ``model_dump_json`` deliberately emits plaintext, because saving the config
        depends on it. That makes it the wrong call for anything shown to a person:
        ``omop-config show`` printed every password and API key in the stack straight
        to the terminal, and into scrollback, screen shares and CI logs with it.

        Masking the rendered structure rather than the model keeps the two concerns
        apart -- serialization stays lossless, display stays safe -- and walking the
        model alongside its dump means the decision still comes from
        :func:`is_sensitive` rather than from key names.
        """
        dumped = self.model_dump(mode="json", exclude_none=exclude_none)
        self._mask_dumped(dumped)
        return json.dumps(dumped, indent=indent)

    def _mask_dumped(self, dumped: Any) -> None:
        """Overwrite every sensitive field of *self* in its own ``model_dump`` result."""
        if not isinstance(dumped, dict):
            return
        for name, info in type(self).model_fields.items():
            if name not in dumped:
                continue
            if is_sensitive(info):
                dumped[name] = MASK
            else:
                _mask_nested(getattr(self, name, None), dumped[name])


def safe_endpoint(url: str | None) -> str | None:
    """Return *url* with every value that could be a credential masked.

    For arbitrary endpoint URLs -- anything rendering
    :attr:`~oa_configurator.domains.llm.schema.ProviderConfig.base_url`, most
    often. :meth:`~oa_configurator.domains.resources.schema.ResolvedConnection.safe_url`
    is the SQLAlchemy-specific equivalent and covers only the password.

    - **Userinfo** (``https://user:pw@host``): the password is masked and the
      username kept, matching ``safe_url``. The username answers "which
      account is this connecting as?", which an operator reading a redacted
      URL needs.
    - **Query string**: every value is masked and every key kept, so
      ``?api-version=2024-02-01&api_key=sk-x`` renders as
      ``?api-version=***&api_key=***``. The operator still sees which
      parameters are set without any value being shown. Dropping the query
      entirely is not an option (Azure OpenAI needs ``api-version``), and
      masking only the keys that look like secrets would be the guess this
      module exists to avoid.
    - **Fragment** (``https://host/v1#access_token=abc``): masked whole, to
      ``#***``. A fragment is never sent to the server, so nothing in an
      endpoint's fragment is operationally meaningful, but the OAuth implicit
      flow delivers access tokens like this, and a fragment has no guaranteed 
      ``key=value`` structure to mask value-by-value. 

    Parameters
    ----------
    url : str, optional
        URL to scrub. ``None`` passes through, so callers can hand this an
        optional config field directly.

    Returns
    -------
    str, optional
        The scrubbed URL, or the bare mask if *url* could not be parsed --
        an unparseable string is scrubbed by refusing to show it at all
        rather than by echoing it back.
    """
    if url is None:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return MASK
    return urlunsplit(
        (
            parts.scheme,
            _mask_userinfo(parts.netloc),
            parts.path,
            _mask_query_values(parts.query),
            MASK if parts.fragment else "",
        )
    )


def _mask_userinfo(netloc: str) -> str:
    """Mask the password in ``user:pw@host:port``, keeping everything else.

    Operates on the raw netloc rather than ``SplitResult.username``/``password``
    so that percent-encoding, IPv6 brackets, and the port survive untouched.
    """
    userinfo, at, host = netloc.rpartition("@")
    if not at:
        return netloc
    user, colon, _password = userinfo.partition(":")
    if not colon:
        return netloc  # username only: nothing here is a secret
    return f"{user}:{MASK}@{host}"


def _mask_query_values(query: str) -> str:
    """Mask the value of every query parameter, keeping every key verbatim.

    Splits on the raw string instead of round-tripping through
    ``parse_qsl``/``urlencode``: that would percent-encode the mask into
    ``%2A%2A%2A`` and re-spell the operator's own keys. A parameter with no
    ``=`` is a bare flag, which is a key with no value to hide.
    """
    if not query:
        return query
    masked = []
    for param in query.split("&"):
        key, eq, _value = param.partition("=")
        masked.append(f"{key}={MASK}" if eq else key)
    return "&".join(masked)


def _mask_nested(value: Any, dumped: Any) -> None:
    """Recurse into containers, pairing each live value with its dumped counterpart."""
    if isinstance(value, SecretSafeBaseModel):
        value._mask_dumped(dumped)
    elif isinstance(value, dict) and isinstance(dumped, dict):
        for key, item in value.items():
            if key in dumped:
                _mask_nested(item, dumped[key])
    elif isinstance(value, list | tuple) and isinstance(dumped, list):
        for item, item_dumped in zip(value, dumped, strict=False):
            _mask_nested(item, item_dumped)


def sanitized_errors(validation_error: ValidationError) -> tuple[ErrorDetails, ...]:
    """Pydantic error details with the rejected input and validator context dropped.

    Error messages reach logs and CI output, so the value must never travel with the
    diagnosis. What survives is the field location and the reason, which is what a
    person needs to fix the file. Every caller that turns a pydantic
    :class:`~pydantic.ValidationError` into user-facing text should route through
    this rather than calling ``validation_error.errors()`` directly, so there is one
    audited answer to what is safe to show.
    """
    return tuple(
        validation_error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
    )


def describe_errors(errors: tuple[ErrorDetails, ...]) -> str:
    """Render sanitized errors as ``field.path: reason`` fragments."""
    problems = []
    for error in errors:
        location = ".".join(str(part) for part in error["loc"])
        prefix = f"{location}: " if location else ""
        problems.append(f"{prefix}{error['msg']}")
    return "; ".join(problems)
