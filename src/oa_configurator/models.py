"""Typed models describing the human-managed stack configuration."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from sqlalchemy.engine import URL

from .logging_config import LoggingConfig
from .paths import resolve_filesystem_path


class _UnsetPasswordOverride:
    """Sentinel for an omitted password override argument."""

    def __repr__(self) -> str:
        return "<UNSET_PASSWORD_OVERRIDE>"


_PASSWORD_UNSET = _UnsetPasswordOverride()
PasswordOverride = str | _UnsetPasswordOverride


class SettingsConfig(BaseModel):
    """Top-level lightweight runtime selection settings.

    ``configuration_base_path`` controls how relative filesystem paths are
    resolved throughout the configuration:

    - ``"."`` means "the fully resolved directory containing the loaded
      configuration file" after the loaded config path has been bound with
      ``StackConfig.bind_loaded_path()`` or by loading through
      ``load_stack_config()``
    - any other value must be an absolute directory path
    """

    model_config = ConfigDict(extra="forbid")

    active_profile: str = "default"
    configuration_base_path: str = "."
    secrets_dir: str | None = None

    def __repr__(self) -> str:
        return (
            "SettingsConfig("
            f"active_profile={self.active_profile!r}, "
            f"configuration_base_path={self.configuration_base_path!r}, "
            f"secrets_dir={self.secrets_dir!r}"
            ")"
        )


class ProfileConfig(BaseModel):
    """Named deployment or usage profile metadata plus optional overlays.

    Profiles can patch named resources and tools without forcing callers to
    duplicate whole base blocks such as ``resources.default`` or
    ``tools.omop_emb``.
    """

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    resource_overrides: dict[str, "ResourceOverrideConfig"] = Field(default_factory=dict)
    tool_overrides: dict[str, "ToolOverrideConfig"] = Field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            "ProfileConfig("
            f"description={self.description!r}, "
            f"resource_overrides={sorted(self.resource_overrides)!r}, "
            f"tool_overrides={sorted(self.tool_overrides)!r}"
            ")"
        )


class ConnectionConfig(BaseModel):
    """Concrete database or file-backed connection target.

    This model captures the minimal details needed to render a URL or file
    reference, while keeping sensitive elements such as passwords out of the
    default developer-facing representation.
    """

    model_config = ConfigDict(extra="forbid")

    dialect: str
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    secret_source: str | None = None
    database: str | None = None
    path: str | None = None
    engine_kwargs: dict[str, Any] = Field(default_factory=dict)
    read_only: bool = False
    kind: Literal["database", "file"] = "database"

    @model_validator(mode="after")
    def validate_shape(self) -> "ConnectionConfig":
        """Validate the minimum fields required by the selected connection type."""

        if self.password is not None and self.secret_source is not None:
            raise ValueError("connections may define password or secret_source, not both")
        if self.kind == "file":
            if self.path is None:
                raise ValueError("file connections require a path")
            for field_name in ("host", "port", "user", "password", "secret_source"):
                if getattr(self, field_name) is not None:
                    raise ValueError(f"file connections may not define {field_name}")
        if self.kind == "database" and self.dialect != "sqlite":
            if self.database is None:
                raise ValueError("database connections require a database name")
        if self.kind != "file" and self.dialect == "sqlite" and self.path is None and self.database is None:
            raise ValueError("sqlite connections require either path or database")
        return self

    def as_url(self) -> str:
        """Render the full connection URL, including password when configured."""

        if self.secret_source is not None and self.password is None:
            raise RuntimeError(
                "ConnectionConfig.as_url() requires a resolved secret when secret_source is configured. "
                "Use Resolver.resolve_connection(...).url or call as_url_resolved(...) with a resolved secret."
            )
        return self.as_url_resolved()

    def as_url_resolved(
        self,
        configuration_base_path: Path | None = None,
        *,
        password_override: PasswordOverride = _PASSWORD_UNSET,
    ) -> str:
        """Render the full connection URL with optional base-path resolution.

        Parameters
        ----------
        configuration_base_path
            Base directory used to resolve relative local file paths. This is
            especially important for sqlite or other file-backed connections.
        """

        file_target = self._resolved_file_target(configuration_base_path)
        if self.kind == "file":
            if file_target is None:
                raise RuntimeError("file connections require a resolved path")
            return f"{self.dialect}:///{file_target}"
        if self.dialect == "sqlite":
            if file_target is not None:
                return f"sqlite:///{file_target}"
            return f"sqlite:///{self.database}"

        return self._render_network_url(
            redact_password=False,
            password_override=password_override,
        )

    def as_safe_url(self) -> str:
        """Render a redacted connection URL suitable for logs and reprs."""

        return self.as_safe_url_resolved()

    def as_safe_url_resolved(
        self,
        configuration_base_path: Path | None = None,
        *,
        password_override: PasswordOverride = _PASSWORD_UNSET,
    ) -> str:
        """Render a redacted connection URL with optional base-path resolution."""

        if self.kind == "file" or self.dialect == "sqlite":
            return self.as_url_resolved(configuration_base_path)

        return self._render_network_url(
            redact_password=True,
            password_override=password_override,
        )

    def engine_create_kwargs(self, **overrides: Any) -> dict[str, Any]:
        """Return effective kwargs for ``sqlalchemy.create_engine()``.

        ``engine_kwargs`` from configuration are used as the base. Explicit
        call-site overrides win. When ``read_only`` is enabled for PostgreSQL,
        a startup ``connect_args.options`` flag is injected unless the caller
        has already set it.
        """

        merged = deepcopy(self.engine_kwargs)
        if self.read_only and self.dialect.startswith("postgresql"):
            connect_args = merged.get("connect_args")
            if connect_args is None:
                connect_args = {}
                merged["connect_args"] = connect_args
            if not isinstance(connect_args, dict):
                raise TypeError(
                    "connections.<name>.engine_kwargs.connect_args must be a mapping "
                    "when read_only=true"
                )
            options = connect_args.get("options")
            if options is None:
                connect_args["options"] = "-c default_transaction_read_only=on"
            elif "default_transaction_read_only" not in str(options):
                connect_args["options"] = (
                    f"{options} -c default_transaction_read_only=on".strip()
                )

        explicit = dict(overrides)
        explicit_connect_args = explicit.pop("connect_args", None)
        if explicit_connect_args is not None and isinstance(merged.get("connect_args"), dict):
            if not isinstance(explicit_connect_args, dict):
                raise TypeError("create_engine(..., connect_args=...) must receive a mapping")
            merged["connect_args"] = {
                **merged["connect_args"],
                **explicit_connect_args,
            }
        elif explicit_connect_args is not None:
            merged["connect_args"] = explicit_connect_args

        merged.update(explicit)
        return merged

    def __repr__(self) -> str:
        path_repr = str(Path(self.path).expanduser()) if self.path is not None else None
        return (
            "ConnectionConfig("
            f"kind={self.kind!r}, "
            f"dialect={self.dialect!r}, "
            f"database={self.database!r}, "
            f"host={self.host!r}, "
            f"port={self.port!r}, "
            f"user={self.user!r}, "
            f"path={path_repr!r}, "
            f"secret_source={self.secret_source!r}, "
            f"engine_kwargs_keys={sorted(self.engine_kwargs)!r}, "
            f"read_only={self.read_only!r}, "
            f"url={self.as_safe_url()!r}"
            ")"
        )

    def _resolved_file_target(self, configuration_base_path: Path | None) -> Path | None:
        """Resolve the configured local file target, if this connection has one."""

        if self.path is None:
            return None
        return resolve_filesystem_path(self.path, configuration_base_path)

    def _render_network_url(
        self,
        *,
        redact_password: bool,
        password_override: PasswordOverride = _PASSWORD_UNSET,
    ) -> str:
        """Render a network-style database URL with optional password redaction."""

        effective_password = self.password if password_override is _PASSWORD_UNSET else password_override
        return URL.create(
            drivername=self.dialect,
            username=self.user,
            password=effective_password if effective_password is not None else None,
            host=self.host or "localhost",
            port=self.port,
            database=self.database or "",
        ).render_as_string(hide_password=redact_password)


class ResourceConfig(BaseModel):
    """Logical resource-role mapping for one stack usage pattern."""

    model_config = ConfigDict(extra="forbid")

    primary_db: str
    vocab_db: str | None = None
    results_db: str | None = None
    omop_schema: str | None = None
    vocab_schema: str | None = None
    results_schema: str | None = None
    athena_source_path: str | None = None
    artifact_root: str | None = None
    embedding_file_root: str | None = None
    analytic_db_file_root: str | None = None

    def __repr__(self) -> str:
        return (
            "ResourceConfig("
            f"primary_db={self.primary_db!r}, "
            f"vocab_db={self.vocab_db!r}, "
            f"results_db={self.results_db!r}, "
            f"omop_schema={self.omop_schema!r}, "
            f"vocab_schema={self.vocab_schema!r}, "
            f"results_schema={self.results_schema!r}, "
            f"embedding_file_root={self.embedding_file_root!r}, "
            f"analytic_db_file_root={self.analytic_db_file_root!r}"
            ")"
        )


class ResourceOverrideConfig(BaseModel):
    """Partial patch for a named :class:`ResourceConfig`.

    Every field is optional. Only explicitly provided values are applied over
    the base resource during resolution for the active profile.
    """

    model_config = ConfigDict(extra="forbid")

    primary_db: str | None = None
    vocab_db: str | None = None
    results_db: str | None = None
    omop_schema: str | None = None
    vocab_schema: str | None = None
    results_schema: str | None = None
    athena_source_path: str | None = None
    artifact_root: str | None = None
    embedding_file_root: str | None = None
    analytic_db_file_root: str | None = None

    def __repr__(self) -> str:
        keys = sorted(self.model_dump(exclude_none=True))
        return f"ResourceOverrideConfig(fields={keys!r})"


class ToolConfig(BaseModel):
    """Tool-specific defaults layered on top of shared resources."""

    model_config = ConfigDict(extra="forbid")

    default_resource: str | None = None
    backend: str | None = None
    embedding_file_root: str | None = None
    database_file_root: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            "ToolConfig("
            f"default_resource={self.default_resource!r}, "
            f"backend={self.backend!r}, "
            f"embedding_file_root={self.embedding_file_root!r}, "
            f"database_file_root={self.database_file_root!r}, "
            f"extra_keys={sorted(self.extra)!r}"
            ")"
        )


class ToolOverrideConfig(BaseModel):
    """Partial patch for a named :class:`ToolConfig`."""

    model_config = ConfigDict(extra="forbid")

    default_resource: str | None = None
    backend: str | None = None
    embedding_file_root: str | None = None
    database_file_root: str | None = None
    extra: dict[str, str] | None = None

    def __repr__(self) -> str:
        keys = sorted(self.model_dump(exclude_none=True))
        return f"ToolOverrideConfig(fields={keys!r})"


class StackConfig(BaseModel):
    """Root typed configuration object for the stack.

    This model is intentionally broad enough to describe:

    - named profiles
    - named concrete connections
    - named logical resources
    - per-tool defaults

    It also exposes a few lightweight discovery helpers so interactive use in
    notebooks and REPLs feels pleasant without exposing sensitive values.
    """

    model_config = ConfigDict(extra="forbid")

    settings: SettingsConfig = Field(default_factory=SettingsConfig)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    connections: dict[str, ConnectionConfig] = Field(default_factory=dict)
    resources: dict[str, ResourceConfig] = Field(default_factory=dict)
    tools: dict[str, ToolConfig] = Field(default_factory=dict)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    _config_file_path: Path | None = PrivateAttr(default=None)
    _resolved_configuration_base_path: Path | None = PrivateAttr(default=None)
    _resolved_secrets_dir: Path | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_references(self) -> "StackConfig":
        """Validate that named cross-references point at configured objects."""

        for resource_name, resource in self.resources.items():
            _validate_resource_connections(
                resource,
                self.connections,
                location=f"resources.{resource_name}",
            )

        for tool_name, tool in self.tools.items():
            _validate_tool_resource(
                tool,
                self.resources,
                location=f"tools.{tool_name}",
            )

        for profile_name, profile in self.profiles.items():
            for resource_name, override in profile.resource_overrides.items():
                base_resource = self.resources.get(resource_name)
                if base_resource is None:
                    raise ValueError(
                        f"profiles.{profile_name}.resource_overrides references unknown resource "
                        f"{resource_name!r}"
                    )
                merged_resource = base_resource.model_copy(
                    update=override.model_dump(exclude_none=True)
                )
                _validate_resource_connections(
                    merged_resource,
                    self.connections,
                    location=f"profiles.{profile_name}.resource_overrides.{resource_name}",
                )

            for tool_name, override in profile.tool_overrides.items():
                base_tool = self.tools.get(tool_name)
                if base_tool is None:
                    raise ValueError(
                        f"profiles.{profile_name}.tool_overrides references unknown tool "
                        f"{tool_name!r}"
                    )
                merged_tool = base_tool.model_copy(
                    update=override.model_dump(exclude_none=True)
                )
                _validate_tool_resource(
                    merged_tool,
                    self.resources,
                    location=f"profiles.{profile_name}.tool_overrides.{tool_name}",
                )

        return self

    @classmethod
    def for_session(
        cls,
        *,
        connections: "dict[str, ConnectionConfig] | None" = None,
        resources: "dict[str, ResourceConfig] | None" = None,
        tools: "dict[str, ToolConfig] | None" = None,
        profiles: "dict[str, ProfileConfig] | None" = None,
        settings: "SettingsConfig | None" = None,
        base_path: Path | None = None,
    ) -> "StackConfig":
        """Construct a StackConfig programmatically without a TOML file.

        The ``base_path`` (default: ``Path.cwd()``) anchors relative filesystem
        paths in connections, resources, and tools. Equivalent to loading a TOML
        file from that directory. Internally this binds a synthetic config file
        path inside ``base_path`` so that ``configuration_base_path = "."``
        behaves the same way it would for a loaded TOML file.

        Example::

            config = StackConfig.for_session(
                connections={"local": ConnectionConfig(dialect="sqlite", database=":memory:")},
                resources={"default": ResourceConfig(primary_db="local")},
            )
            engine = Resolver(config).resolve_resource("default").create_engine()
        """
        config = cls(
            connections=connections or {},
            resources=resources or {},
            tools=tools or {},
            profiles=profiles or {},
            settings=settings or SettingsConfig(),
        )
        resolved_base = (base_path or Path.cwd()).expanduser().resolve()
        config.bind_loaded_path(resolved_base / "_session.toml")
        return config

    def bind_loaded_path(self, config_file_path: Path) -> None:
        """Bind the resolved loaded config path and derive path resolution base.

        This should be called by the loader after the TOML file is parsed so
        that every later filesystem resolution uses a stable directory base
        rather than the process working directory.

        The method only uses the fully resolved file path and its parent
        directory; the file itself does not need to exist. ``for_session()``
        relies on that behavior by binding a synthetic ``_session.toml`` path
        within the chosen base directory.
        """

        resolved_file = config_file_path.expanduser().resolve()
        configured_base = Path(self.settings.configuration_base_path).expanduser()
        if self.settings.configuration_base_path == ".":
            resolved_base = resolved_file.parent
        else:
            if not configured_base.is_absolute():
                raise ValueError(
                    "settings.configuration_base_path must be '.' or an absolute path. "
                    f"Got {self.settings.configuration_base_path!r}."
                )
            resolved_base = configured_base.resolve()

        self._config_file_path = resolved_file
        self._resolved_configuration_base_path = resolved_base
        if self.settings.secrets_dir is None:
            self._resolved_secrets_dir = None
        else:
            self._resolved_secrets_dir = resolve_filesystem_path(
                self.settings.secrets_dir,
                resolved_base,
            )

    @property
    def config_file_path(self) -> Path | None:
        """Return the fully resolved path of the loaded TOML file, if bound."""

        return self._config_file_path

    @property
    def configuration_base_path(self) -> Path:
        """Return the fully resolved directory used for relative path expansion."""

        if self._resolved_configuration_base_path is None:
            configured_base = Path(self.settings.configuration_base_path).expanduser()
            if self.settings.configuration_base_path == ".":
                raise RuntimeError(
                    "configuration_base_path is '.' but no config file has been bound yet. "
                    "Load configuration through load_stack_config() or call bind_loaded_path()."
                )
            if not configured_base.is_absolute():
                raise RuntimeError(
                    "configuration_base_path must be '.' or an absolute path."
                )
            return configured_base.resolve()
        return self._resolved_configuration_base_path

    @property
    def secrets_dir(self) -> Path | None:
        """Return the fully resolved directory used for file-backed secrets."""

        if self.settings.secrets_dir is None:
            return None
        if self._resolved_secrets_dir is None:
            return resolve_filesystem_path(
                self.settings.secrets_dir,
                self.configuration_base_path,
            )
        return self._resolved_secrets_dir

    def profile_names(self) -> tuple[str, ...]:
        """Return known profile names in sorted order."""

        return tuple(sorted(self.profiles))

    def connection_names(self) -> tuple[str, ...]:
        """Return known non-secret connection names in sorted order."""

        return tuple(sorted(self.connections))

    def resource_names(self) -> tuple[str, ...]:
        """Return known logical resource names in sorted order."""

        return tuple(sorted(self.resources))

    def tool_names(self) -> tuple[str, ...]:
        """Return known tool-default names in sorted order."""

        return tuple(sorted(self.tools))

    def active_profile_config(self) -> ProfileConfig | None:
        """Return the configured active profile object, if present."""

        return self.profiles.get(self.settings.active_profile)

    def __repr__(self) -> str:
        if self._resolved_configuration_base_path is not None:
            config_base_repr = str(self._resolved_configuration_base_path)
        else:
            config_base_repr = self.settings.configuration_base_path
        secrets_dir_repr = str(self._resolved_secrets_dir) if self._resolved_secrets_dir is not None else self.settings.secrets_dir
        return (
            "StackConfig("
            f"active_profile={self.settings.active_profile!r}, "
            f"configuration_base_path={config_base_repr!r}, "
            f"secrets_dir={secrets_dir_repr!r}, "
            f"profiles={len(self.profiles)}, "
            f"connections={len(self.connections)}, "
            f"resources={len(self.resources)}, "
            f"tools={len(self.tools)}"
            ")"
        )


def _validate_resource_connections(
    resource: ResourceConfig,
    connections: dict[str, ConnectionConfig],
    *,
    location: str,
) -> None:
    """Raise when a resource references a missing named connection."""

    for field_name, connection_name in (
        ("primary_db", resource.primary_db),
        ("vocab_db", resource.vocab_db),
        ("results_db", resource.results_db),
    ):
        if connection_name is not None and connection_name not in connections:
            raise ValueError(
                f"{location}.{field_name} references unknown connection "
                f"{connection_name!r}"
            )


def _validate_tool_resource(
    tool: ToolConfig,
    resources: dict[str, ResourceConfig],
    *,
    location: str,
) -> None:
    """Raise when a tool references a missing named resource."""

    if tool.default_resource is not None and tool.default_resource not in resources:
        raise ValueError(
            f"{location}.default_resource references unknown resource "
            f"{tool.default_resource!r}"
        )
