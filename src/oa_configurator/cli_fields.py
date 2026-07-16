from dataclasses import dataclass
from typing import Callable

import click
import typer

from .models import ResourceConfig, DatabaseConfig
from .package_base import ResourceSpec


@dataclass(frozen=True)
class FieldSpec:
    """Definition for a single CLI field (e.g. a DatabaseConfig or ResourceConfig field).

    Parameters
    ----------
    name : str
        The name of the field, matching a DatabaseConfig/ResourceConfig field.
    label : str
        A human friendly label for the field, shown in interactive prompts and --help text.
    default : str or Callable[[ResourceSpec], str], optional
        A default value for the field, used as the prompt default and non-interactive
        fallback. If a callable is given, it is called with the ResourceSpec to derive
        a default value.
    nullable : bool, default True
        Whether the field can be null. If False, an empty resolved value is treated as
        an error in non-interactive mode. See resolve_fields.
    hide_input : bool, default False
        Whether to hide input when prompting for this field (e.g. for passwords).
    """

    name: str
    label: str
    default: str | Callable[[ResourceSpec], str] = ""
    nullable: bool = True
    hide_input: bool = False


TEST_FLAG_PREFIX = "test"


def flag_name(name: str, prefix: str = "") -> str:
    """Build a Click flag name from a field name and an optional prefix.

    Parameters
    ----------
    name : str
        The field name, e.g. "database_name". Underscores become hyphens.
    prefix : str, optional
        A prefix to insert before the field name, without a trailing hyphen
        (e.g. "test", not "test-"). The hyphen is appended automatically when
        prefix is non-empty. Default: ""

    Returns
    -------
    str
        The flag name, e.g. "--database-name" or "--test-database-name".
    """
    full_prefix = f"{prefix}-" if prefix else ""
    return f"--{full_prefix}{name.replace('_', '-')}"


# -------------------
# Database connection
# -------------------
FS_DATABASE = FieldSpec(
    "database",
    "Database label (short name for this database entry, e.g. 'cdm')",
    default=lambda spec: spec.connection_name_hint or spec.semantic_name,
    nullable=False,
)
FS_DIALECT = FieldSpec(
    "dialect",
    "Dialect (SQLAlchemy driver string, e.g. postgresql+psycopg, sqlite)",
    nullable=False,
)
FS_HOST = FieldSpec(
    "host",
    "Host (e.g. Docker container name)",
    default="localhost",
    nullable=False,
)
FS_PORT = FieldSpec(
    "port",
    "Port (leave blank to use the dialect default)",
)
FS_USER = FieldSpec(
    "user",
    "User (leave blank if not required)",
)
FS_PASSWORD = FieldSpec(
    "password",
    "Password (leave blank if not required)",
    hide_input=True,
)
FS_DATABASE_NAME = FieldSpec(
    "database_name",
    "Database name (name of the database on the server, or file path for SQLite)",
    nullable=False,
)

DATABASE_LABEL_FIELDS: tuple[FieldSpec, ...] = (
    FS_DATABASE,
    FS_DIALECT,
    FS_HOST,
    FS_PORT,
    FS_USER,
    FS_PASSWORD,
    FS_DATABASE_NAME,
)

# --------------------
# Schema configuration
# --------------------
FS_CDM_SCHEMA = FieldSpec(
    "cdm_schema",
    "CDM schema (schema containing the OMOP tables)",
    default=lambda spec: spec.cdm_schema_default,
    nullable=False,
)
FS_VOCAB_SCHEMA = FieldSpec(
    "vocab_schema",
    "Vocab schema (blank = same schema as CDM; set only if vocabulary lives in a separate schema)",
)
FS_RESULTS_SCHEMA = FieldSpec(
    "results_schema",
    "Results schema (for Achilles/Atlas results tables; blank = not used)",
)
FS_NON_CDM_SCHEMA = FieldSpec(
    FS_CDM_SCHEMA.name,
    "Schema (leave blank for public/default)",
    default=lambda spec: spec.cdm_schema_default,
    nullable=False,
)
CDM_SCHEMA_FIELDS = (FS_CDM_SCHEMA, FS_VOCAB_SCHEMA, FS_RESULTS_SCHEMA)
NON_CDM_SCHEMA_FIELDS = (FS_NON_CDM_SCHEMA,)


def resolve_field_value(
    field_name: str,
    *,
    prompt_label: str,
    default_value: str,
    flags: dict[str, str],
    stored: dict[str, str],
    spec_defaults: dict[str, str] | None,
    non_interactive: bool,
    missing_required: list[str],
    required: bool = False,
    hide_input: bool = False,
) -> str:
    """Resolve one field's value.

    Parameters
    ----------
    field_name : str
        The field name to resolve, used to look it up in flags/stored/spec_defaults.
    prompt_label : str
        The label shown to the user in an interactive prompt.
    default_value : str
        The value shown (and accepted on Enter) in an interactive prompt, and the
        silent non-interactive fallback when nothing else supplied a value.
    flags : dict[str, str]
        Explicit CLI flag values, keyed by field name. Checked first.
    stored : dict[str, str]
        Values already stored in config.toml for this resource. Checked second.
    spec_defaults : dict[str, str] or None
        Package-supplied defaults for this resource (ResourceSpec.connection_defaults,
        stringified). Checked third.
    non_interactive : bool
        Whether to skip prompting and fall back to default_value instead.
    missing_required : list[str]
        Mutable list that field_name is appended to when required is True,
        non_interactive is True, and nothing else resolved a value.
    required : bool, default False
        Whether this field has no usable fallback. Computed by resolve_fields; should
        not be set by hand elsewhere.
    hide_input : bool, default False
        Whether to hide input when prompting (e.g. for passwords).

    Returns
    -------
    str
        The resolved value: explicit flag, then stored config, then spec default,
        then either an interactive prompt or, non-interactively, default_value.
    """
    if (val := flags.get(field_name)) is not None:
        return str(val)
    if field_name in stored:
        return stored[field_name]
    if spec_defaults and (pre := spec_defaults.get(field_name)) is not None:
        return str(pre)
    if non_interactive:
        if required:
            missing_required.append(field_name)
        return default_value
    return typer.prompt(prompt_label, default=default_value, hide_input=hide_input)


def resolve_fields(
    fields: tuple[FieldSpec, ...],
    spec: ResourceSpec,
    resolve_field_fn: Callable[..., str],
) -> dict[str, str | None]:
    """Resolve a whole field table at once via resolve (a resolve_field_value partial).

    Parameters
    ----------
    fields : tuple[FieldSpec, ...]
        The field table to resolve, e.g. DATABASE_LABEL_FIELDS.
    spec : ResourceSpec
        The resource being configured, passed to any callable FieldSpec.default.
    resolve_field_fn : Callable[..., str]
        A resolve_field_value partial, pre-bound with flags/stored/spec_defaults/etc.

    Returns
    -------
    dict[str, str or None]
        One entry per field, keyed by FieldSpec.name. A field counts as required
        (reported via resolve's missing_required list when nothing resolves it)
        exactly when it is not nullable and has no usable default: not
        callable(f.default) and not f.nullable and not f.default. A callable
        default always counts as usable, since it is written to always return a
        real value.
    """
    result: dict[str, str | None] = {}
    for f in fields:
        default = f.default(spec) if callable(f.default) else f.default  # ty: ignore[call-top-callable]
        is_required = not callable(f.default) and not f.nullable and not f.default
        value = resolve_field_fn(f.name, prompt_label=f.label, default_value=default, required=is_required, hide_input=f.hide_input)
        result[f.name] = (value or None) if f.nullable else value
    return result


def pop_str(values: dict[str, str | None], name: str) -> str:
    """Pop a field that resolve_fields has already guaranteed is not None.

    Parameters
    ----------
    values : dict[str, str or None]
        A dict produced by resolve_fields.
    name : str
        The field name to pop. Must be one whose FieldSpec has nullable=False.

    Returns
    -------
    str
        The popped value.
    """
    value = values.pop(name)
    assert value is not None
    return value


def build_resource_params(spec: ResourceSpec, *, prefix: str = "") -> list[click.Parameter]:
    """Generate Click options for one resource from its field tables.

    Parameters
    ----------
    spec : ResourceSpec
        The resource to generate flags for.
    prefix : str, optional
        A prefix for the flag names, without a trailing hyphen (e.g. "test", not
        "test-"), used to prevent namespace collisions between a package's owned
        resource and its test resource, which often share field names (host, port,
        etc.). Default: ""

    Returns
    -------
    list[click.Parameter]
        One click.Option per field in DATABASE_LABEL_FIELDS plus the resource's schema
        fields (CDM_SCHEMA_FIELDS or NON_CDM_SCHEMA_FIELDS, depending on
        spec.is_cdm_database).
    """
    schema_fields = CDM_SCHEMA_FIELDS if spec.is_cdm_database else NON_CDM_SCHEMA_FIELDS

    return [
        click.Option(
            [flag_name(f.name, prefix)],
            default=None,
            type=click.STRING,
            help=f.label,
        )
        for f in (*DATABASE_LABEL_FIELDS, *schema_fields)
    ]


def build_resource_config(
    database: str,
    schema_values: dict[str, str | None],
    is_cdm_database: bool,
) -> ResourceConfig:
    """Build a ResourceConfig from resolve_fields output for a schema field table.

    Parameters
    ----------
    database : str
        The database label this resource points to.
    schema_values : dict[str, str or None]
        Output of resolve_fields(CDM_SCHEMA_FIELDS or NON_CDM_SCHEMA_FIELDS, ...).
    is_cdm_database : bool
        Whether this resource is a CDM database. Non-CDM resources have no
        vocab_schema or results_schema.

    Returns
    -------
    ResourceConfig
    """
    cdm_schema = pop_str(schema_values, FS_CDM_SCHEMA.name)
    return ResourceConfig(
        database=database,
        cdm_schema=cdm_schema,
        vocab_schema=schema_values.get(FS_VOCAB_SCHEMA.name) if is_cdm_database else None,
        results_schema=schema_values.get(FS_RESULTS_SCHEMA.name) if is_cdm_database else None,
    )


def build_database_config(
    conn_values: dict[str, str | None],
) -> tuple[DatabaseConfig, str]:
    """Build a DatabaseConfig from resolve_fields(DATABASE_LABEL_FIELDS, ...) output.

    Parameters
    ----------
    conn_values : dict[str, str or None]
        Output of resolve_fields(DATABASE_LABEL_FIELDS, ...). Must be resolved, and
        checked for missing required fields, before this is called, since popping
        a field with no value raises rather than reporting it.

    Returns
    -------
    DatabaseConfig
    str
        The database label this config should be stored under.
    """
    database = pop_str(conn_values, FS_DATABASE.name)
    dialect = pop_str(conn_values, FS_DIALECT.name)
    raw_port = conn_values.pop(FS_PORT.name)
    db_config = DatabaseConfig(
        dialect=dialect,
        host=conn_values[FS_HOST.name],
        port=int(raw_port) if raw_port is not None else None,
        user=conn_values[FS_USER.name],
        password=conn_values[FS_PASSWORD.name],
        database_name=conn_values[FS_DATABASE_NAME.name],
    )
    return db_config, database