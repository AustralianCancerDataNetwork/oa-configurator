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
        required_resources: ClassVar[tuple[str, ...]] = ("cdm_db",)
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

from .models import DatabaseConfig, StackConfig


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


class ConfigurationError(ValueError):
    """Raised when a required resource or connection is missing from the stack config."""


class PackageConfigBase(BaseModel):
    """Typed view over a package's ``[tools.<tool_name>]`` TOML section.

    Subclass this and declare the class variables below. Users who name their
    resource differently can add a ``[resource_aliases]`` section to
    config.toml (e.g. ``cdm_db = "my_prod"``) so all packages resolve
    correctly without per-package ``default_resource`` overrides.

    Attributes
    ----------
    tool_name : str
        Key used in ``[tools.<name>]``. Must be set on every subclass.
    required_resources : tuple[str, ...]
        Canonical resource names this package depends on. A missing resource
        at :meth:`from_stack` time raises :exc:`ConfigurationError`.
    owned_resources : tuple[ResourceSpec, ...]
        Resources this package is responsible for configuring interactively.
        ``omop-config configure`` prompts for these before package extras.
    test_resources : tuple[ResourceSpec, ...]
        Optional test-only resources. ``omop-config configure`` presents a
        Y/N prompt for these after the main resource flow. Marked with a
        DROP SCHEMA warning; a collision check prevents pointing at any
        already-configured non-test resource.
    extra_logging_namespaces : tuple[str, ...]
        Logger namespaces of transitive dependencies to configure alongside
        this package. The package's own ``tool_name``
        are always included -> only list additional roots here, e.g.
        ``("<my_extra_package_to_log",)``. Missing namespaces are harmless.
    """

    tool_name: ClassVar[str]
    required_resources: ClassVar[tuple[str, ...]] = ()
    owned_resources: ClassVar[tuple[ResourceSpec, ...]] = ()
    test_resources: ClassVar[tuple[ResourceSpec, ...]] = ()
    extra_logging_namespaces: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def from_stack(cls, config: StackConfig) -> Self:
        """Load this package's section from a :class:`StackConfig`.

        Validates that all :attr:`required_resources` (or the
        ``default_resource`` override) are present in the config before
        instantiating. Alias resolution via ``config.resource_aliases`` is
        applied before the existence check. Raises :exc:`ConfigurationError`
        with an actionable message if any are missing.
        """
        tool = config.tools.get(cls.tool_name)
        override = tool.default_resource if tool else None

        available: set[str] = set(config.resource_names())
        if config.active_profile and config.active_profile in config.profiles:
            available |= set(config.profiles[config.active_profile].resources)

        # default_resource is a user-level alias for required_resources[0]. Treat it as a
        # substitute for the first entry but still validate the rest of required_resources.
        if override and cls.required_resources:
            names_to_check: list[str] = [override] + list(cls.required_resources[1:])
        else:
            names_to_check = list(cls.required_resources)
        for check_name in names_to_check:
            resolved_check = config.resource_aliases.get(check_name, check_name)
            if resolved_check not in available:
                alias_hint = (
                    f"\nTip: if you named your resource differently, add:\n"
                    f"  [resource_aliases]\n  {check_name} = \"your-resource-name\""
                )
                raise ConfigurationError(
                    f"{cls.__name__} requires resource {check_name!r} "
                    f"but it is not configured.\n"
                    f"Available: {sorted(available) or '(none)'}\n"
                    f"Run 'omop-config configure {cls.tool_name}' to set up your configuration."
                    + alias_hint
                )

        return cls.model_validate(tool.extra if tool else {})

    @classmethod
    def get_config(cls) -> Self:
        """Load this package's config from the active stack config file."""
        from .loader import load_stack_config
        return cls.from_stack(load_stack_config())

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
    def get_engine(cls, resource_name: str | None = None, **engine_kwargs: Any) -> Any:
        """Create a SQLAlchemy engine for a resource.

        Parameters
        ----------
        resource_name:
            Resource to resolve. If ``None``, uses ``tool.default_resource``
            from the config file, or falls back to ``required_resources[0]``.
        **engine_kwargs:
            Forwarded to :meth:`~oa_configurator.resolver.ResolvedResource.create_engine`.

        Raises
        ------
        ConfigurationError
            If no resource name can be determined.
        """
        from .loader import load_stack_config
        from .resolver import Resolver
        stack = load_stack_config()
        tool = stack.tools.get(cls.tool_name)
        name = resource_name or (tool.default_resource if tool else None) or (
            cls.required_resources[0] if cls.required_resources else None
        )
        if name is None:
            raise ConfigurationError(f"{cls.__name__} has no resources to resolve.")
        return Resolver(stack).resolve_resource(name).create_engine(**engine_kwargs)

    def to_extra_dict(self) -> dict[str, Any]:
        """Serialize back to the dict stored in ``ToolConfig.extra``."""
        return self.model_dump(exclude_none=True)
