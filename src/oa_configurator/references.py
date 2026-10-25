"""Cross-references between the entries of a stack config.

Fields marked with :class:`~oa_configurator.refs.RefTo` name an entry in
another section. This module reads those markers to answer the forward
question (does a model's references resolve?), the reverse question (which
fields name a given entry?), and to rename an entry together with every
reference to it.

``[tools.*]`` values are untyped dictionaries, so finding their ``RefTo``
fields needs the matching :class:`~oa_configurator.package_base.PackageConfigBase`
class. A tool section without a known class is reported, never treated as
holding no references.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from .domains.llm.schema import ModelConfig, ProviderConfig
from .domains.resources.schema import CDMDatabaseConfig, ConnectionConfig, GenericDatabaseConfig
from .domains.vector_stores.schema import VectorStoreConfig
from .package_base import ConfigurationError, PackageConfigBase, package_entry_points
from .refs import RefTo, _iter_refs

if TYPE_CHECKING:
    from .stack_config import StackConfig

# Which StackConfig dict a RefTo(target) marker resolves against,
# and is allowed to reference. Deliberately exclude abstract base classes
# like DatabaseConfig, as they are not meant to be constructed directly.

_REF_SECTIONS: dict[type[BaseModel], str] = {
    ConnectionConfig: "connections",
    ProviderConfig: "providers",
    ModelConfig: "models",
    GenericDatabaseConfig: "databases",
    CDMDatabaseConfig: "databases",
    VectorStoreConfig: "vector_stores",
}

_ENTRY_SECTIONS = tuple(dict.fromkeys(_REF_SECTIONS.values()))


class UnknownRefTarget(TypeError):
    """A RefTo marker names a class that isn't registered in _REF_SECTIONS.

    Indicates a bug in the declaring package's own schema (e.g. `RefTo`
    against an abstract base like `DatabaseConfig`, or a class that was
    never meant to be a RefTo target at all), not a user configuration
    problem.
    """


def _ref_section(target: type[BaseModel], *, field_name: str | None = None) -> str:
    """Look up which StackConfig section a RefTo(target) resolves against.

    Raises :class:`UnknownRefTarget` with the field name (when known) and
    the list of valid targets, instead of letting a bare ``KeyError``
    surface a raw class repr.
    """
    try:
        return _REF_SECTIONS[target]
    except KeyError:
        valid = ", ".join(sorted(t.__name__ for t in _REF_SECTIONS))
        where = f"field {field_name!r} " if field_name else ""
        raise UnknownRefTarget(
            f"RefTo {where}targets {target.__name__!r}, which isn't a registered "
            f"RefTo section. Valid targets: {valid}."
        ) from None


def _set_refs(
    cls: type[BaseModel], value_of: Callable[[str], Any]
) -> Iterator[tuple[str, RefTo, Any]]:
    """Yield (field_name, RefTo, value) for every RefTo field of *cls* with a value.

    Values are read through *value_of*, so the same walk serves model
    instances and the untyped dictionaries of ``[tools.*]`` sections.
    """
    for field_name, ref in _iter_refs(cls):
        value = value_of(field_name)
        if value is not None:
            yield field_name, ref, value


def unresolved_refs(instance: BaseModel, config: StackConfig) -> list[tuple[str, str, str]]:
    """Find every RefTo-marked field on instance whose value doesn't resolve
    against config.

    Checks existence only. A value that exists but is the wrong concrete
    subtype, for example a RefTo(CDMDatabaseConfig) field pointing at a
    GenericDatabaseConfig entry, is not unresolved. See
    :func:`mismatched_kind_refs` for that, checked separately so the two
    failure modes get distinct, correctly actionable wording.

    Parameters
    ----------
    instance : BaseModel
        The object whose RefTo-marked fields are being checked.
    config : StackConfig
        The stack config to resolve field values against.

    Returns
    -------
    list[tuple[str, str, str]]
        One (field_name, value, section) triple per unresolved field.
        section is the StackConfig attribute the value should have been
        found in, e.g. "connections".
    """
    problems: list[tuple[str, str, str]] = []
    for field_name, ref, value in _set_refs(type(instance), partial(getattr, instance)):
        section = _ref_section(ref.target, field_name=field_name)
        if value not in getattr(config, section):
            problems.append((field_name, value, section))
    return problems


def mismatched_kind_refs(
    instance: BaseModel, config: StackConfig
) -> list[tuple[str, str, type[BaseModel], type[BaseModel]]]:
    """Find every RefTo-marked field on instance whose value names an
    existing entry of the wrong concrete subtype.

    Parameters
    ----------
    instance : BaseModel
        The object whose RefTo-marked fields are being checked.
    config : StackConfig
        The stack config to resolve field values against.

    Returns
    -------
    list[tuple[str, str, type[BaseModel], type[BaseModel]]]
        One (field_name, value, expected_type, actual_type) tuple per
        mismatched field.
    """
    problems: list[tuple[str, str, type[BaseModel], type[BaseModel]]] = []
    for field_name, ref, value in _set_refs(type(instance), partial(getattr, instance)):
        if not isinstance(ref.target, type):
            continue
        section = _ref_section(ref.target, field_name=field_name)
        entry = getattr(config, section).get(value)
        if entry is not None and not isinstance(entry, ref.target):
            problems.append((field_name, value, ref.target, type(entry)))
    return problems


@dataclass(frozen=True)
class Reference:
    """One field that names a target entry.

    Parameters
    ----------
    source_section : str
        Section holding the referring entry, e.g. ``"databases"`` or ``"tools"``.
    source_name : str
        Name of the referring entry, or the tool name for ``"tools"``.
    field : str
        Name of the referring field.
    target_section : str
        Section the field points into.
    target_name : str
        Name of the entry the field points at.
    """

    source_section: str
    source_name: str
    field: str
    target_section: str
    target_name: str


class DiscoveryProblemKind(StrEnum):
    """The type of problem found during discovery."""

    BROKEN_IMPORT = "broken_import"
    DUPLICATE_NAME = "duplicate_name"
    NOT_A_PACKAGE_CONFIG = "not_a_package_config"


@dataclass(frozen=True)
class DiscoveryProblem:
    """A package registered under ``omop.config`` that could not be used.

    Parameters
    ----------
    kind : DiscoveryProblemKind
        What went wrong.
    name : str
        Entry-point name, or the duplicated tool name.
    detail : str
        Short description of the failure.
    """

    kind: DiscoveryProblemKind
    name: str
    detail: str


@dataclass(frozen=True)
class ReferenceReport:
    """Result of a reference lookup.

    Parameters
    ----------
    references : tuple of Reference
        Every known field matching the query, ordered by source section, name
        and field.
    unknown_tool_schemas : tuple of str
        Names of ``[tools.*]`` sections with no known package class. They may
        hold references the lookup could not see.
    discovery_problems : tuple of DiscoveryProblem
        Entry-point problems found while discovering package classes. Empty
        when the caller supplied the registry explicitly.
    """

    references: tuple[Reference, ...]
    unknown_tool_schemas: tuple[str, ...] = ()
    discovery_problems: tuple[DiscoveryProblem, ...] = ()


def _tool_values(
    package_config: type[PackageConfigBase], stored: Mapping[str, Any]
) -> Callable[[str], Any]:
    """Read a field from a ``[tools.*]`` section, falling back to the class default."""

    def value_of(field_name: str) -> Any:
        default = package_config.model_fields[field_name].default
        return stored.get(field_name, None if default is PydanticUndefined else default)

    return value_of


def collect_references(
    config: StackConfig, *, package_configs: Mapping[str, type[PackageConfigBase]]
) -> ReferenceReport:
    """List every reference in the stack, i.e. each field that names another entry.
    Lists both the forward and reverse directions for references. Filter on the source
    for what an entry uses, on the target for what uses it.

    No file I/O and no entry-point discovery. A package field whose section omits
    it refers to its declared default, so that default counts as a reference.

    Parameters
    ----------
    config : StackConfig
        The stack to read.
    package_configs : Mapping[str, type[PackageConfigBase]]
        Known package classes keyed by ``tool_name``.

    Returns
    -------
    ReferenceReport
        All references found and the ``[tools.*]`` sections that could not be
        inspected.
    """
    found: list[Reference] = []
    for source_section in _ENTRY_SECTIONS:
        for source_name, entry in getattr(config, source_section).items():
            for field, ref, value in _set_refs(type(entry), partial(getattr, entry)):
                target_section = _ref_section(ref.target, field_name=field)
                found.append(Reference(source_section, source_name, field, target_section, value))

    unknown: list[str] = []
    for tool_name, stored in config.tools.items():
        package_config = package_configs.get(tool_name)
        if package_config is None:
            unknown.append(tool_name)
            continue
        for field, ref, value in _set_refs(package_config, _tool_values(package_config, stored)):
            if isinstance(value, str):
                target_section = _ref_section(ref.target, field_name=field)
                found.append(Reference("tools", tool_name, field, target_section, value))

    found.sort(key=lambda r: (r.source_section, r.source_name, r.field))
    return ReferenceReport(tuple(found), tuple(sorted(unknown)))


def references_to(
    config: StackConfig,
    section: str,
    name: str,
    *,
    package_configs: Mapping[str, type[PackageConfigBase]],
) -> ReferenceReport:
    """Find every entry and package field that names ``section.name``.

    Parameters
    ----------
    config : StackConfig
        The stack to search.
    section : str
        Target section, e.g. ``"connections"``.
    name : str
        Target entry name. It need not exist in *section*.
    package_configs : Mapping[str, type[PackageConfigBase]]
        Known package classes keyed by ``tool_name``.

    Returns
    -------
    ReferenceReport
        The references to the target and the ``[tools.*]`` sections that
        could not be inspected.

    Raises
    ------
    ValueError
        If *section* is not a section that entries can be referenced in.
    """
    if section not in _ENTRY_SECTIONS:
        raise ValueError(
            f"Cannot reference into {section!r}. Valid sections: {', '.join(_ENTRY_SECTIONS)}"
        )
    report = collect_references(config, package_configs=package_configs)
    matching = tuple(
        r for r in report.references if (r.target_section, r.target_name) == (section, name)
    )
    return replace(report, references=matching)


def discover_package_configs() -> tuple[
    dict[str, type[PackageConfigBase]], tuple[DiscoveryProblem, ...]
]:
    """Load the package classes registered in the ``omop.config`` entry-point group.

    A name registered by two different classes is ambiguous, so neither is
    returned. Every entry point that cannot be used is reported rather than
    skipped.

    Returns
    -------
    package_configs : dict[str, type[PackageConfigBase]]
        Usable classes keyed by ``tool_name``.
    problems : tuple of DiscoveryProblem
        Entry points that could not be used.

    Raises
    ------
    Exception
        Anything other than ``ImportError`` or ``AttributeError`` raised
        while loading a package propagates.
    """
    loaded: dict[str, list[type[PackageConfigBase]]] = {}
    problems: list[DiscoveryProblem] = []
    for ep in package_entry_points():
        try:
            candidate = ep.load()
        except (ImportError, AttributeError) as exc:
            problems.append(
                DiscoveryProblem(DiscoveryProblemKind.BROKEN_IMPORT, ep.name, type(exc).__name__)
            )
            continue
        if not (isinstance(candidate, type) and issubclass(candidate, PackageConfigBase)):
            problems.append(
                DiscoveryProblem(
                    DiscoveryProblemKind.NOT_A_PACKAGE_CONFIG, ep.name, repr(candidate)
                )
            )
            continue
        classes = loaded.setdefault(candidate.tool_name, [])
        if candidate not in classes:
            classes.append(candidate)

    package_configs: dict[str, type[PackageConfigBase]] = {}
    for tool_name, classes in loaded.items():
        if len(classes) == 1:
            package_configs[tool_name] = classes[0]
        else:
            names = ", ".join(sorted(f"{c.__module__}.{c.__qualname__}" for c in classes))
            problems.append(DiscoveryProblem(DiscoveryProblemKind.DUPLICATE_NAME, tool_name, names))
    return package_configs, tuple(problems)


def find_references(config: StackConfig, section: str, name: str) -> ReferenceReport:
    """Obtains class package configs and returns a report of every reference to ``section.name``.
    Scans the `omop.config` entry-point group for package classes, so the result may include discovery problems.

    Parameters
    ----------
    config : StackConfig
        The stack to search.
    section : str
        Target section, e.g. ``"connections"``.
    name : str
        Target entry name.

    Returns
    -------
    ReferenceReport
        Includes ``discovery_problems``. A tool section whose package is not
        installed, failed to import or is registered ambiguously appears in
        ``unknown_tool_schemas``.
    """
    package_configs, problems = discover_package_configs()
    report = references_to(config, section, name, package_configs=package_configs)
    return replace(report, discovery_problems=problems)


def rename_entry(
    config: StackConfig,
    section: str,
    old: str,
    new: str,
    *,
    package_configs: Mapping[str, type[PackageConfigBase]],
) -> StackConfig:
    """Return a validated copy with ``section.old`` renamed and every reference repointed.

    Pure: no file I/O, and *config* is not modified. Persisting the result is
    a separate :func:`~oa_configurator.io.save_stack_config` call. The entry
    keeps its position in the section. A package field that omitted its value
    and so referred to *old* through its default gets an explicit *new* value,
    because the default would otherwise no longer resolve. The candidate is
    validated as a whole: forward references, database kinds, and the package
    sections that were repointed.

    Parameters
    ----------
    config : StackConfig
        The stack to rename in.
    section : str
        Section holding the entry, e.g. ``"connections"``.
    old : str
        Current entry name.
    new : str
        New entry name.
    package_configs : Mapping[str, type[PackageConfigBase]]
        Known package classes keyed by ``tool_name``, used to find references
        inside ``[tools.*]``.

    Returns
    -------
    StackConfig
        The complete renamed candidate, with the input's ``loaded_path``.

    Raises
    ------
    ConfigurationError
        If *section* cannot hold renameable entries, *old* does not exist,
        *new* is empty, unchanged or already taken, or any ``[tools.*]``
        section has no known package class, since it could hold a reference
        that would be left dangling.
    PackageConfigInvalidError
        If a repointed package section fails its schema.
    """
    if section not in _ENTRY_SECTIONS:
        raise ConfigurationError(
            f"Cannot rename in {section!r}. Valid sections: {', '.join(_ENTRY_SECTIONS)}"
        )
    entries = getattr(config, section)
    if old not in entries:
        raise ConfigurationError(f"No {section[:-1]} named {old!r} to rename")
    if not new.strip() or new != new.strip():
        raise ConfigurationError(f"{new!r} is not a valid {section[:-1]} name")
    if new == old:
        raise ConfigurationError(f"{section[:-1].capitalize()} {old!r} already has that name")
    if new in entries:
        raise ConfigurationError(f"A {section[:-1]} named {new!r} already exists")

    report = references_to(config, section, old, package_configs=package_configs)
    if report.unknown_tool_schemas:
        raise ConfigurationError(
            "Cannot rename safely: no known package class for "
            + ", ".join(f"[tools.{name}]" for name in report.unknown_tool_schemas)
        )

    candidate = config.model_copy(deep=True)
    candidate_entries = getattr(candidate, section)
    renamed = {(new if key == old else key): value for key, value in candidate_entries.items()}
    candidate_entries.clear()
    candidate_entries.update(renamed)

    repointed_tools: set[str] = set()
    for ref in report.references:
        if ref.source_section == "tools":
            candidate.tools[ref.source_name][ref.field] = new
            repointed_tools.add(ref.source_name)
            continue
        source_name = (
            new if (ref.source_section, ref.source_name) == (section, old) else ref.source_name
        )
        setattr(getattr(candidate, ref.source_section)[source_name], ref.field, new)

    planned = type(config).model_validate(candidate.model_dump(mode="python"))
    for tool_name in sorted(repointed_tools):
        package_configs[tool_name].validate_candidate(planned)
    if config.loaded_path is not None:
        planned.bind_loaded_path(config.loaded_path)
    return planned
