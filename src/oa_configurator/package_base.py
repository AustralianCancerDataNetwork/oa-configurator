"""Base class for per-package configuration sections.

Each package that wants to support ``omop-config configure <name>`` subclasses
:class:`PackageConfigBase`, declares its typed fields, and registers itself via
the ``omop.config`` entry-point group in its ``pyproject.toml``.

Example
-------
In ``<my-package>/config.py``::

    from oa_configurator import PackageConfigBase
    from typing import ClassVar

    class MyPackageConfig(PackageConfigBase):
        tool_name: ClassVar[str] = "<my-package>"
        required_resources: ClassVar[tuple[str, ...]] = ("cdm_db",)  # owned by this package
        # or, for a resource owned by another package:
        # required_resources: ClassVar[tuple[ResourceRef, ...]] = (
        #     ResourceRef(OtherPackageConfig, OtherPackageConfig.SOME_RESOURCE),
        # )
        <additional typed fields here>

    config = MyPackageConfig.get_config()

In ``pyproject.toml``::

    [project.entry-points."omop.config"]
    <my-package> = "<my-package>.config:<MyPackageConfig>"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pydantic import BaseModel

from .models import DatabaseConfig


@dataclass(frozen=True)
class ResourceSpec:
    """Declares a resource that a package owns and can configure interactively.

    Packages that own a resource (i.e. they are responsible for prompting the
    user to set it up) add instances to ``owned_resources`` on their
    ``PackageConfigBase`` subclass.  The ``omop-config configure`` command
    reads this tuple and invokes the connection + schema prompts for each
    spec before asking for package-specific extras.

    ``owned_resources`` is a CLI-only concern; it has no effect at runtime.

    Attributes
    ----------
    cdm_schema_default
        Default value for the schema prompt. Defaults to ``"omop"`` for OMOP
        CDM databases. Set to ``"public"`` for non-CDM databases (e.g. the
        pgvector embedding database).
    is_cdm_database
        When ``False``, the configure prompt skips the vocab schema and
        results schema questions — those are OMOP CDM-specific concepts that
        do not apply to other databases such as the pgvector embedding store.
    connection_defaults
        Optional pre-fill values for the connection prompts (dialect, host, port,
        user, password, database_name), as a ``DatabaseConfig`` instance. Only the
        fields actually set are used; the rest fall through to each field's own
        default. Applied when there is no stored config for a field.
    """

    semantic_name: str
    display_name: str
    description: str
    connection_name_hint: str = ""
    cdm_schema_default: str = "omop"
    is_cdm_database: bool = True
    connection_defaults: DatabaseConfig | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ResourceRef:
    """Typed reference to a resource owned by another package.

    Used in ``required_resources`` when the resource is declared (via
    ``owned_resources``) on a *different* package's ``PackageConfigBase``
    subclass, instead of matching against a bare semantic-name string.
    Carries both where to look (``owning_class.tool_name``, for actionable
    error messages) and precisely what for (``spec``, since the owning class
    may declare more than one resource).

    Resolves directly to ``spec.semantic_name`` -- the owning package's
    resource must be configured under that literal name. Renaming an owned
    resource for cross-package consumption (e.g. via a saved-name pointer on
    the owning package's own tool config) is not yet supported.
    """

    owning_class: type["PackageConfigBase"]
    spec: ResourceSpec


@dataclass(frozen=True)
class ModelFieldSpec:
    """Marks one of a package's own fields as naming a ``[models.*]`` entry.

    Packages with a field like ``embedding_model_name: str`` add a matching
    entry to ``referenced_models`` on their ``PackageConfigBase`` subclass.
    ``omop-config configure`` then resolves that field by offering to reuse
    an existing ``[models.*]`` entry or create one on the spot -- recursing
    into ``[providers.*]`` the same way when the chosen provider doesn't
    exist either -- instead of blindly prompting for a raw string.

    ``referenced_models`` is a CLI-only concern; it has no effect at runtime.
    The package's own field stays a plain ``str`` naming the resolved entry.
    """

    field_name: str
    display_name: str
    description: str


class ConfigurationError(ValueError):
    """Raised when a required resource or connection is missing from the stack config."""


class PackageConfigBase(BaseModel):
    """Typed view over a package's ``[tools.<tool_name>]`` TOML section.

    Subclass this and declare the class variables below.

    Attributes
    ----------
    tool_name : str
        Key used in ``[tools.<name>]``. Must be set on every subclass.
    required_resources : tuple[ResourceRef | ResourceSpec | str, ...]
        Resources this package depends on. A ``ResourceSpec`` (or bare
        semantic-name ``str``) means a resource this package owns itself; a
        ``ResourceRef`` means a resource owned by another package. A missing
        resource at :meth:`~oa_configurator.Resolver.resolve_package_config`
        time raises :exc:`ConfigurationError`.
    owned_resources : tuple[ResourceSpec, ...]
        Resources this package is responsible for configuring interactively.
        ``omop-config configure`` prompts for these before package extras.
    test_resources : tuple[ResourceSpec, ...]
        Optional test-only resources. ``omop-config configure`` presents a
        Y/N prompt for these after the main resource flow. Marked with a
        DROP SCHEMA warning; a collision check prevents pointing at any
        already-configured non-test resource.
    referenced_models : tuple[ModelFieldSpec, ...]
        Package fields that name a ``[models.*]`` entry. ``omop-config
        configure`` resolves these interactively (reuse or create, recursing
        into ``[providers.*]``) instead of prompting for a raw string.
    extra_logging_namespaces : tuple[str, ...]
        Logger namespaces of transitive dependencies to configure alongside
        this package. The package's own ``tool_name``
        are always included -> only list additional roots here, e.g.
        ``("<my_extra_package_to_log",)``. Missing namespaces are harmless.
    """

    tool_name: ClassVar[str]
    required_resources: ClassVar[tuple[ResourceRef | ResourceSpec | str, ...]] = ()
    owned_resources: ClassVar[tuple[ResourceSpec, ...]] = ()
    test_resources: ClassVar[tuple[ResourceSpec, ...]] = ()
    referenced_models: ClassVar[tuple[ModelFieldSpec, ...]] = ()
    extra_logging_namespaces: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def get_config(cls) -> Self:
        """Load this package's config from the active stack config file."""
        from .resolver import Resolver
        return Resolver.from_active_config().resolve_package_config(cls)

    @classmethod
    def configure_logging(cls, config=None, *, verbosity: int = 0, console=None) -> None:
        """Configure logging for this package and its declared transitive dependencies."""
        from .logging_config import configure_logging as _configure_logging
        _configure_logging(
            config,
            verbosity=verbosity,
            extra_namespaces=list(cls.extra_logging_namespaces) + [cls.tool_name],
            console=console,
        )

    @classmethod
    def get_engine(cls, resource: "ResourceRef | ResourceSpec | str", **engine_kwargs: Any) -> Any:
        """Create a SQLAlchemy engine for a resource.

        Parameters
        ----------
        resource:
            Resource to resolve: a ``ResourceRef`` (owned by another
            package), a ``ResourceSpec`` (owned by this package), or a bare
            resource name. No zero-argument defaulting -- pass the resource
            explicitly.
        **engine_kwargs:
            Forwarded to :meth:`~oa_configurator.resolver.ResolvedResource.create_engine`.
        """
        from .resolver import Resolver
        return Resolver.from_active_config().resolve_engine(resource, **engine_kwargs)

    def to_extra_dict(self) -> dict[str, Any]:
        """Serialize back to the dict stored in ``ToolConfig.extra``."""
        return self.model_dump(exclude_none=True)
