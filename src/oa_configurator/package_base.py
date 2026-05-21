"""Base class for per-package configuration sections.

Each package that wants to support ``omop-config configure <name>`` subclasses
:class:`PackageConfigBase`, declares its typed fields, and registers itself via
the ``omop.config`` entry-point group in its ``pyproject.toml``.

Example
-------
In ``omop_emb/config.py``::

    from oa_configurator import PackageConfigBase
    from typing import ClassVar

    class OmopEmbConfig(PackageConfigBase):
        tool_name: ClassVar[str] = "omop_emb"
        backend: str = "sqlitevec"
        embedding_file_root: str | None = None

    def get_config() -> OmopEmbConfig:
        from oa_configurator import load_stack_config
        return OmopEmbConfig.from_stack(load_stack_config())

In ``pyproject.toml``::

    [project.entry-points."omop.config"]
    omop_emb = "omop_emb.config:OmopEmbConfig"
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from .models import StackConfig


class PackageConfigBase(BaseModel):
    """Typed view over a package's ``[tools.<tool_name>]`` TOML section.

    Subclass this and set ``tool_name`` to the key used in
    ``[tools.<name>]``. Fields declared on the subclass are validated against
    the tool's ``extra`` dict when loaded via :meth:`from_stack`.
    """

    tool_name: ClassVar[str]  # must be set on every subclass

    @classmethod
    def from_stack(cls, config: StackConfig) -> "PackageConfigBase":
        """Load this package's section from a :class:`StackConfig`.

        If no ``[tools.<tool_name>]`` section exists the class is instantiated
        with all defaults, which lets packages work with a minimal config.
        """
        tool = config.tools.get(cls.tool_name)
        return cls.model_validate(tool.extra if tool else {})

    def to_extra_dict(self) -> dict[str, Any]:
        """Serialize back to the dict stored in ``ToolConfig.extra``."""
        return self.model_dump(exclude_none=True)
