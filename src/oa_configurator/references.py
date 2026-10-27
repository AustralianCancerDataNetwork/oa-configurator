"""Cross-references between the entries of a stack config.

Fields marked with :class:`RefTo` name an entry in
another section. This module reads those markers to answer the forward
question (does a model's references resolve?), the reverse question (which
fields name a given entry?), and to rename an entry together with every
reference to it. It also clones and deletes entries with the same checks.

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
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

from .package_base import ConfigurationError, PackageConfigBase, package_entry_points
from .sensitive import describe_errors, sanitized_errors

if TYPE_CHECKING:
    from .stack_config import StackConfig


class Sectioned(BaseModel):
    """Base for models stored as entries of a StackConfig section.

    Attributes
    ----------
    section : ClassVar[str]
        Name of the :class:`~oa_configurator.stack_config.StackConfig` section
        that holds entries of this class, e.g. ``"connections"``.
    """

    section: ClassVar[str]


class Referenceable(Sectioned):
    """Marks a concrete entry class that a :class:`RefTo` may name.

    Sectioned classes that are abstract, such as ``DatabaseConfig``, leave
    this mixin out and so are rejected as ``RefTo`` targets.
    """


@dataclass(frozen=True)
class RefTo:
    """Marks a string field as naming an entry in another top-level section.

    Applied via e.g. ``Annotated[str, RefTo(ConnectionConfig)]``. One generic
    marker drives both cross-reference validation (:func:`unresolved_refs`)
    and the CLI wizard's reuse-or-create recursion for a consuming package's
    own fields (e.g. ``embedding_model_name: Annotated[str, RefTo(ModelConfig)]``).

    Attributes
    ----------
    target : type[Referenceable]
        The entry class this field's value should name; its ``section`` says
        where the entry lives.
    is_test : bool
        Whether this field is expected to resolve to a ``test_only``
        connection (for ``target=DatabaseConfig`` fields). Replaces a
        previous, unreliable convention of inferring this from whether the
        field's own Python name started with ``"test_"``. Drives both the
        CLI wizard's "Test database (optional)" prompt and the symmetric
        ``is_test``/``test_only`` match enforced by
        :meth:`~oa_configurator.resolver.Resolver.resolve_package_config`.
    """

    target: type[Referenceable]
    is_test: bool = False


class UnknownRefTarget(TypeError):
    """A RefTo marker names a class that is not :class:`Referenceable`.

    Indicates a bug in the declaring package's own schema (e.g. `RefTo`
    against an abstract base like `DatabaseConfig`, or a class that was
    never meant to be a RefTo target at all), not a user configuration
    problem.
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


def _referenceable_classes(base: type[Referenceable] = Referenceable) -> set[type[Referenceable]]:
    """Every subclass of *base*."""
    found: set[type[Referenceable]] = set()
    for subclass in base.__subclasses__():
        found.add(subclass)
        found |= _referenceable_classes(subclass)
    return found


def _ref_section(target: type[BaseModel], *, field_name: str | None = None) -> str:
    """Return the StackConfig section a RefTo(target) resolves against.

    Raises :class:`UnknownRefTarget` with the field name (when known) and
    the list of valid targets when *target* is not :class:`Referenceable`.
    """
    if not (isinstance(target, type) and issubclass(target, Referenceable)):
        valid = ", ".join(sorted(c.__name__ for c in _referenceable_classes()))
        where = f"field {field_name!r} " if field_name else ""
        raise UnknownRefTarget(
            f"RefTo {where}targets {target.__name__!r}, which isn't a Referenceable "
            f"entry class. Valid targets: {valid}."
        ) from None
    return target.section


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
    for source_section in config.entry_sections():
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
    if section not in config.entry_sections():
        raise ValueError(
            f"Cannot reference into {section!r}. Valid sections: {', '.join(config.entry_sections())}"
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


def _entries_for(config: StackConfig, section: str, action: str) -> dict[str, Any]:
    """Return the entries of *section*, or raise if it holds no named entries."""
    if section not in config.entry_sections():
        raise ConfigurationError(
            f"Cannot {action} in {section!r}. Valid sections: {', '.join(config.entry_sections())}"
        )
    return getattr(config, section)


def _require_new_name(entries: Mapping[str, Any], section: str, new: str) -> None:
    """Raise unless *new* is a usable, unused entry name for *section*."""
    if not new.strip() or new != new.strip():
        raise ConfigurationError(f"{new!r} is not a valid {section[:-1]} name")
    if new in entries:
        raise ConfigurationError(f"A {section[:-1]} named {new!r} already exists")


def _require_known_tool_schemas(report: ReferenceReport, action: str) -> None:
    """Raise if any tool section could not be inspected for references."""
    if report.unknown_tool_schemas:
        raise ConfigurationError(
            f"Cannot {action} safely: no known package class for "
            + ", ".join(f"[tools.{name}]" for name in report.unknown_tool_schemas)
        )


def _validated_copy(config: StackConfig, candidate: StackConfig) -> StackConfig:
    """Revalidate *candidate* as a whole, keeping the loaded path of *config*.

    A rejection is reported from the sanitized errors, so a rejected value such
    as a password never appears in the message.
    """
    try:
        planned = type(config).model_validate(candidate.model_dump(mode="python"))
    except ValidationError as exc:
        raise ConfigurationError(
            "; ".join(
                (f"{'.'.join(str(part) for part in error['loc'])}: " if error["loc"] else "")
                + error["msg"].removeprefix("Value error, ")
                for error in sanitized_errors(exc)
            )
        ) from None
    if config.loaded_path is not None:
        planned.bind_loaded_path(config.loaded_path)
    return planned


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
    entries = _entries_for(config, section, "rename")
    if old not in entries:
        raise ConfigurationError(f"No {section[:-1]} named {old!r} to rename")
    if new == old:
        raise ConfigurationError(f"{section[:-1].capitalize()} {old!r} already has that name")
    _require_new_name(entries, section, new)

    report = references_to(config, section, old, package_configs=package_configs)
    _require_known_tool_schemas(report, "rename")

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

    planned = _validated_copy(config, candidate)
    for tool_name in sorted(repointed_tools):
        package_configs[tool_name].validate_candidate(planned)
    return planned


def clone_entry(
    config: StackConfig,
    section: str,
    source: str,
    new: str,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> StackConfig:
    """Return a validated copy with ``section.source`` copied to ``section.new``.

    Pure: no file I/O, and *config* is not modified. The copy is added at the
    end of the section. The source is left unchanged, and nothing that points
    at it is repointed.

    An unchanged copy is often invalid, for example two CDM databases on one
    connection or two identical connections. Use *overrides* to change fields
    of the copy. The whole stack is validated, so a copy that conflicts with
    another entry is refused.

    Parameters
    ----------
    config : StackConfig
        The stack to clone in.
    section : str
        Section holding the entry, e.g. ``"databases"``.
    source : str
        Name of the entry to copy.
    new : str
        Name of the copy.
    overrides : Mapping[str, Any], optional
        Field values for the copy, keyed by the entry's own field names.
        Validated by the entry's class, so an unknown field or a bad value is
        refused.

    Returns
    -------
    StackConfig
        The complete candidate, with the input's ``loaded_path``.

    Raises
    ------
    ConfigurationError
        If *section* holds no named entries, *source* does not exist, *new* is
        empty or already taken, an override is invalid, or the candidate fails
        validation. Messages never include rejected values.
    """
    entries = _entries_for(config, section, "clone")
    if source not in entries:
        raise ConfigurationError(f"No {section[:-1]} named {source!r} to clone")
    _require_new_name(entries, section, new)

    original = entries[source]
    try:
        entry = type(original).model_validate(
            {**original.model_dump(mode="python"), **(overrides or {})}
        )
    except ValidationError as exc:
        raise ConfigurationError(
            f"Invalid override for {section[:-1]} {new!r}: "
            + describe_errors(sanitized_errors(exc))
        ) from None

    candidate = config.model_copy(deep=True)
    getattr(candidate, section)[new] = entry
    return _validated_copy(config, candidate)


def delete_entry(
    config: StackConfig,
    section: str,
    name: str,
    *,
    package_configs: Mapping[str, type[PackageConfigBase]],
) -> StackConfig:
    """Return a validated copy without ``section.name``, if nothing references it.

    Pure: no file I/O, and *config* is not modified. There is no way to force
    a deletion that leaves dangling references.

    Parameters
    ----------
    config : StackConfig
        The stack to delete from.
    section : str
        Section holding the entry, e.g. ``"connections"``.
    name : str
        Name of the entry to delete.
    package_configs : Mapping[str, type[PackageConfigBase]]
        Known package classes keyed by ``tool_name``, used to find references
        inside ``[tools.*]``.

    Returns
    -------
    StackConfig
        The candidate without the entry, with the input's ``loaded_path``.

    Raises
    ------
    ConfigurationError
        If *section* holds no named entries, *name* does not exist, any field
        still references the entry (listed in the message), or any
        ``[tools.*]`` section has no known package class and so cannot be
        shown to be free of references. A package field that omits its value
        and refers to the entry through its default counts as a reference.
    """
    entries = _entries_for(config, section, "delete")
    if name not in entries:
        raise ConfigurationError(f"No {section[:-1]} named {name!r} to delete")

    report = references_to(config, section, name, package_configs=package_configs)
    _require_known_tool_schemas(report, "delete")
    if report.references:
        raise ConfigurationError(
            f"Cannot delete {section[:-1]} {name!r}: still referenced by "
            + ", ".join(f"{r.source_section}.{r.source_name}.{r.field}" for r in report.references)
        )

    candidate = config.model_copy(deep=True)
    del getattr(candidate, section)[name]
    return _validated_copy(config, candidate)


def remove_tool_section(config: StackConfig, tool_name: str) -> StackConfig:
    """Return a validated copy without the ``[tools.<tool_name>]`` section.

    Pure: no file I/O, and *config* is not modified. Nothing refers to a tool
    section, so removal needs no package class and works for a section whose
    package is broken or no longer installed.

    Parameters
    ----------
    config : StackConfig
        The stack to remove the section from.
    tool_name : str
        Name of the tool section.

    Returns
    -------
    StackConfig
        The candidate without the section, with the input's ``loaded_path``.

    Raises
    ------
    ConfigurationError
        If the stack has no such tool section.
    """
    if tool_name not in config.tools:
        raise ConfigurationError(f"No tool section named {tool_name!r} to remove")
    candidate = config.model_copy(deep=True)
    del candidate.tools[tool_name]
    return _validated_copy(config, candidate)
