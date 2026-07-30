from dataclasses import dataclass
from typing import Callable

import click
import typer

from .models import ModelConfig, ProviderConfig, ResourceConfig, DatabaseConfig
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


class FS_Database:
    """Field table for a `[databases.<name>]` connection.

    Grouped as a plain namespace (never instantiated) rather than
    module-level constants, so a field is always reached through its
    section, e.g. ``FS_Database.HOST`` instead of a bare ``FS_HOST``.
    """

    LABEL = FieldSpec(
        "database",
        "Database label (short name for this database entry, e.g. 'cdm')",
        default=lambda spec: spec.connection_name_hint or spec.semantic_name,
        nullable=False,
    )
    DIALECT = FieldSpec(
        "dialect",
        "Dialect (SQLAlchemy driver string, e.g. postgresql+psycopg, sqlite)",
        nullable=False,
    )
    HOST = FieldSpec(
        "host",
        "Host (e.g. Docker container name)",
        default="localhost",
        nullable=False,
    )
    PORT = FieldSpec(
        "port",
        "Port (leave blank to use the dialect default)",
    )
    USER = FieldSpec(
        "user",
        "User (leave blank if not required)",
    )
    PASSWORD = FieldSpec(
        "password",
        "Password (leave blank if not required)",
        hide_input=True,
    )
    DATABASE_NAME = FieldSpec(
        "database_name",
        "Database name (name of the database on the server, or file path for SQLite)",
        nullable=False,
    )
    ALL: tuple[FieldSpec, ...] = (LABEL, DIALECT, HOST, PORT, USER, PASSWORD, DATABASE_NAME)


class FS_Schema:
    """Field tables for a resource's schema configuration.

    ``CDM`` applies to OMOP CDM databases (vocab/results schema fall back to
    ``cdm_schema``); ``NON_CDM`` applies to non-CDM resources (e.g. the
    pgvector embedding store), which have no vocab/results concept.
    """

    CDM_SCHEMA = FieldSpec(
        "cdm_schema",
        "CDM schema (schema containing the OMOP tables)",
        default=lambda spec: spec.cdm_schema_default,
        nullable=False,
    )
    VOCAB_SCHEMA = FieldSpec(
        "vocab_schema",
        "Vocab schema (blank = same schema as CDM; set only if vocabulary lives in a separate schema)",
    )
    RESULTS_SCHEMA = FieldSpec(
        "results_schema",
        "Results schema (for Achilles/Atlas results tables; blank = not used)",
    )
    NON_CDM_SCHEMA = FieldSpec(
        CDM_SCHEMA.name,
        "Schema (leave blank for public/default)",
        default=lambda spec: spec.cdm_schema_default,
        nullable=False,
    )
    CDM: tuple[FieldSpec, ...] = (CDM_SCHEMA, VOCAB_SCHEMA, RESULTS_SCHEMA)
    NON_CDM: tuple[FieldSpec, ...] = (NON_CDM_SCHEMA,)


class FS_Provider:
    """Field table for a `[providers.<name>]` entry."""

    KEY = FieldSpec(
        "provider",
        "Provider key (e.g. ollama, llamacpp, vllm, openai, anthropic, gemini)",
        nullable=False,
    )
    BASE_URL = FieldSpec(
        "base_url",
        "Base URL (leave blank to use the provider's default)",
    )
    API_KEY = FieldSpec(
        "api_key",
        "API key (leave blank if not required)",
        hide_input=True,
    )
    ALL: tuple[FieldSpec, ...] = (KEY, BASE_URL, API_KEY)


class FS_Model:
    """Field table for a `[models.<name>]` entry."""

    PROVIDER_REF = FieldSpec(
        "provider",
        "Provider name (an entry from [providers])",
        nullable=False,
    )
    NAME = FieldSpec(
        "model",
        "Model name/identifier passed to the provider",
        nullable=False,
    )
    EMBEDDING_DIM = FieldSpec(
        "embedding_dim",
        "Embedding dimension (leave blank to auto-discover, or if not an embedding model)",
    )
    DOCUMENT_PREFIX = FieldSpec(
        "document_prefix",
        "Document embedding prefix (leave blank for symmetric models)",
    )
    QUERY_PREFIX = FieldSpec(
        "query_prefix",
        "Query embedding prefix (leave blank for symmetric models)",
    )
    ALL: tuple[FieldSpec, ...] = (PROVIDER_REF, NAME, EMBEDDING_DIM, DOCUMENT_PREFIX, QUERY_PREFIX)


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
    spec: ResourceSpec | None,
    resolve_field_fn: Callable[..., str],
) -> dict[str, str | None]:
    """Resolve a whole field table at once via resolve (a resolve_field_value partial).

    Parameters
    ----------
    fields : tuple[FieldSpec, ...]
        The field table to resolve, e.g. FS_Database.ALL.
    spec : ResourceSpec or None
        The resource being configured, passed to any callable FieldSpec.default.
        None for field tables with no callable defaults (e.g. FS_Provider.ALL,
        FS_Model.ALL), which are not resource-owned.
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
        if callable(f.default):
            assert spec is not None, f"{f.name} has a callable default but no spec was given"
            default = f.default(spec)  # ty: ignore[call-top-callable]
        else:
            default = f.default
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
        One click.Option per field in FS_Database.ALL plus the resource's schema
        fields (FS_Schema.CDM or FS_Schema.NON_CDM, depending on
        spec.is_cdm_database).
    """
    schema_fields = FS_Schema.CDM if spec.is_cdm_database else FS_Schema.NON_CDM

    return [
        click.Option(
            [flag_name(f.name, prefix)],
            default=None,
            type=click.STRING,
            help=f.label,
        )
        for f in (*FS_Database.ALL, *schema_fields)
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
        Output of resolve_fields(FS_Schema.CDM or FS_Schema.NON_CDM, ...).
    is_cdm_database : bool
        Whether this resource is a CDM database. Non-CDM resources have no
        vocab_schema or results_schema.

    Returns
    -------
    ResourceConfig
    """
    cdm_schema = pop_str(schema_values, FS_Schema.CDM_SCHEMA.name)
    return ResourceConfig(
        database=database,
        cdm_schema=cdm_schema,
        vocab_schema=schema_values.get(FS_Schema.VOCAB_SCHEMA.name) if is_cdm_database else None,
        results_schema=schema_values.get(FS_Schema.RESULTS_SCHEMA.name) if is_cdm_database else None,
    )


def build_database_config(
    conn_values: dict[str, str | None],
) -> tuple[DatabaseConfig, str]:
    """Build a DatabaseConfig from resolve_fields(FS_Database.ALL, ...) output.

    Parameters
    ----------
    conn_values : dict[str, str or None]
        Output of resolve_fields(FS_Database.ALL, ...). Must be resolved, and
        checked for missing required fields, before this is called, since popping
        a field with no value raises rather than reporting it.

    Returns
    -------
    DatabaseConfig
    str
        The database label this config should be stored under.
    """
    database = pop_str(conn_values, FS_Database.LABEL.name)
    dialect = pop_str(conn_values, FS_Database.DIALECT.name)
    raw_port = conn_values.pop(FS_Database.PORT.name)
    db_config = DatabaseConfig(
        dialect=dialect,
        host=conn_values[FS_Database.HOST.name],
        port=int(raw_port) if raw_port is not None else None,
        user=conn_values[FS_Database.USER.name],
        password=conn_values[FS_Database.PASSWORD.name],
        database_name=conn_values[FS_Database.DATABASE_NAME.name],
    )
    return db_config, database


def build_provider_config(values: dict[str, str | None]) -> ProviderConfig:
    """Build a ProviderConfig from resolve_fields(FS_Provider.ALL, ...) output.

    Parameters
    ----------
    values : dict[str, str or None]
        Output of resolve_fields(FS_Provider.ALL, None, ...).

    Returns
    -------
    ProviderConfig
    """
    provider = pop_str(values, FS_Provider.KEY.name)
    return ProviderConfig(
        provider=provider,
        base_url=values[FS_Provider.BASE_URL.name],
        api_key=values[FS_Provider.API_KEY.name],
    )


def build_model_config(values: dict[str, str | None]) -> ModelConfig:
    """Build a ModelConfig from resolve_fields(FS_Model.ALL, ...) output.

    Parameters
    ----------
    values : dict[str, str or None]
        Output of resolve_fields(FS_Model.ALL, None, ...).

    Returns
    -------
    ModelConfig
    """
    provider = pop_str(values, FS_Model.PROVIDER_REF.name)
    model = pop_str(values, FS_Model.NAME.name)
    raw_dim = values[FS_Model.EMBEDDING_DIM.name]
    return ModelConfig(
        provider=provider,
        model=model,
        embedding_dim=int(raw_dim) if raw_dim is not None else None,
        document_prefix=values[FS_Model.DOCUMENT_PREFIX.name],
        query_prefix=values[FS_Model.QUERY_PREFIX.name],
    )
