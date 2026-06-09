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

from dataclasses import dataclass
from typing import Any, ClassVar, Self

from pydantic import BaseModel

from .models import StackConfig


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
    """

    semantic_name: str
    display_name: str
    description: str
    connection_name_hint: str = ""
    cdm_schema_default: str = "omop"
    is_cdm_database: bool = True


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
    extra_logging_namespaces : tuple[str, ...]
        Logger namespaces of transitive dependencies to configure alongside
        this package. The package's own ``tool_name``
        are always included -> only list additional roots here, e.g.
        ``("<my_extra_package_to_log",)``. Missing namespaces are harmless.
    """

    tool_name: ClassVar[str]
    required_resources: ClassVar[tuple[str, ...]] = ()
    owned_resources: ClassVar[tuple[ResourceSpec, ...]] = ()
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
        check_name = override or (cls.required_resources[0] if cls.required_resources else None)

        if check_name:
            available: set[str] = set(config.resource_names())
            if config.active_profile and config.active_profile in config.profiles:
                available |= set(config.profiles[config.active_profile].resources)
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
    def configure_logging(cls, config=None, *, verbosity: int = 0) -> None:
        """Configure logging for this package and its declared transitive dependencies."""
        from .logging_config import configure_logging as _configure_logging
        _configure_logging(
            config,
            verbosity=verbosity,
            extra_namespaces=list(cls.extra_logging_namespaces) + [cls.tool_name],
        )

    def to_extra_dict(self) -> dict[str, Any]:
        """Serialize back to the dict stored in ``ToolConfig.extra``."""
        return self.model_dump(exclude_none=True)
