"""Tests for cli.py: connections/databases/providers/models add/list commands."""

from __future__ import annotations

import pytest
import typer
from pydantic import Field
from typer.testing import CliRunner

from typing import Annotated, ClassVar

import oa_configurator.cli as cli
from oa_configurator import (
    CDMDatabaseConfig,
    ConnectionConfig,
    GenericDatabaseConfig,
    ModelConfig,
    ProviderConfig,
    RefTo,
    StackConfig,
    VectorStoreConfig,
)
from oa_configurator.io import save_stack_config as _real_save_stack_config
from oa_configurator.loader import _load_from_path
from oa_configurator.package_base import PackageConfigBase
from oa_configurator.resolver import _resolve_ref

runner = CliRunner()


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect the active config path/load/save to an isolated tmp_path file.

    Patches both cli.py's own imported bindings (used by its direct
    commands, e.g. init/_add_entry/_list_entries) and the source
    oa_configurator.loader/io module attributes (used by
    PackageConfigBase.run_configure's lazy, call-time imports), so both
    paths land on the same isolated file.
    """
    import oa_configurator.io as io_mod
    import oa_configurator.loader as loader_mod

    config_path = tmp_path / "config.toml"
    load = lambda: _load_from_path(config_path)  # noqa: E731
    save = lambda config: _real_save_stack_config(config, path=config_path)  # noqa: E731

    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "load_stack_config", load)
    monkeypatch.setattr(cli, "save_stack_config", save)
    monkeypatch.setattr(loader_mod, "CONFIG_PATH", config_path)
    monkeypatch.setattr(loader_mod, "load_stack_config", load)
    monkeypatch.setattr(io_mod, "save_stack_config", save)
    return config_path


def _seed(config_path, config: StackConfig) -> None:
    _real_save_stack_config(config, path=config_path)


class TestConnectionsAdd:
    def test_non_interactive_creates_connection(self, isolated_config):
        result = runner.invoke(
            cli.app,
            [
                "connections",
                "add",
                "cdm",
                "--dialect",
                "sqlite",
                "--database-name",
                ":memory:",
            ],
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.connections["cdm"].dialect == "sqlite"
        assert config.connections["cdm"].database_name == ":memory:"

    def test_non_interactive_missing_required_field_fails(self, isolated_config):
        result = runner.invoke(
            cli.app, ["connections", "add", "cdm", "--host", "localhost"]
        )
        assert result.exit_code != 0
        assert not isolated_config.exists()

    def test_update_existing_connection(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={"cdm": ConnectionConfig(dialect="sqlite")}
            ),
        )
        result = runner.invoke(
            cli.app, ["connections", "add", "cdm", "--host", "otherhost"]
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.connections["cdm"].dialect == "sqlite"
        assert config.connections["cdm"].host == "otherhost"

    def test_test_only_flag_sets_bool_field(self, isolated_config):
        result = runner.invoke(
            cli.app,
            [
                "connections",
                "add",
                "test_cdm",
                "--dialect",
                "sqlite",
                "--test-only",
                "true",
            ],
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.connections["test_cdm"].test_only is True

    def test_test_only_flag_accepts_false_variants(self, isolated_config):
        result = runner.invoke(
            cli.app,
            ["connections", "add", "cdm", "--dialect", "sqlite", "--test-only", "no"],
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.connections["cdm"].test_only is False


class TestConnectionsList:
    def test_empty(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session())
        result = runner.invoke(cli.app, ["connections", "list"])
        assert result.exit_code == 0
        assert "No connections configured" in result.output

    def test_lists_configured_connections(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={"cdm": ConnectionConfig(dialect="sqlite")}
            ),
        )
        result = runner.invoke(cli.app, ["connections", "list"])
        assert result.exit_code == 0
        assert "cdm" in result.output
        assert "sqlite" in result.output

    def test_lists_test_only_column(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={
                    "test_cdm": ConnectionConfig(dialect="sqlite", test_only=True)
                },
            ),
        )
        result = runner.invoke(cli.app, ["connections", "list"])
        assert result.exit_code == 0
        assert "test_only" in result.output
        assert "True" in result.output


class TestDatabasesAdd:
    def test_non_interactive_creates_database(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={"cdm": ConnectionConfig(dialect="sqlite")}
            ),
        )
        result = runner.invoke(
            cli.app,
            [
                "databases",
                "add",
                "cdm_db",
                "--kind",
                "cdm",
                "--connection",
                "cdm",
                "--schema-name",
                "omop",
            ],
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.databases["cdm_db"].connection == "cdm"
        assert config.databases["cdm_db"].schema_name == "omop"

    def test_non_interactive_creates_generic_database(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={"emb": ConnectionConfig(dialect="sqlite")}
            ),
        )
        result = runner.invoke(
            cli.app,
            ["databases", "add", "emb_db", "--kind", "generic", "--connection", "emb"],
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.databases["emb_db"].connection == "emb"
        assert config.databases["emb_db"].schema_name is None

    def test_missing_kind_with_other_flags_fails(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={"cdm": ConnectionConfig(dialect="sqlite")}
            ),
        )
        result = runner.invoke(
            cli.app, ["databases", "add", "cdm_db", "--connection", "cdm"]
        )
        assert result.exit_code != 0
        assert "--kind is required" in result.output

    def test_unknown_connection_reference_fails(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session())
        result = runner.invoke(
            cli.app,
            [
                "databases",
                "add",
                "cdm_db",
                "--kind",
                "generic",
                "--connection",
                "does-not-exist",
            ],
        )
        assert result.exit_code != 0
        config = _load_from_path(isolated_config)
        assert "cdm_db" not in config.databases

    def test_kind_change_refused_when_a_vector_store_depends_on_it(
        self, isolated_config
    ):
        """emb_db is a GenericDatabaseConfig a vector store depends on
        (RefTo(GenericDatabaseConfig)). Re-adding it as --kind cdm would
        turn that reference into a mismatched-kind error every time the
        config loads -- must be refused up front instead."""
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={
                    "c": ConnectionConfig(dialect="sqlite", database_name=":memory:")
                },
                databases={"emb_db": GenericDatabaseConfig(connection="c")},
                vector_stores={
                    "vs": VectorStoreConfig(backend_type="pgvector", database="emb_db")
                },
            ),
        )
        result = runner.invoke(
            cli.app,
            ["databases", "add", "emb_db", "--kind", "cdm", "--connection", "c"],
        )
        assert result.exit_code != 0
        config = _load_from_path(isolated_config)
        assert isinstance(config.databases["emb_db"], GenericDatabaseConfig)

    def test_kind_change_warns_when_nothing_depends_on_it(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={
                    "c": ConnectionConfig(dialect="sqlite", database_name=":memory:")
                },
                databases={"emb_db": GenericDatabaseConfig(connection="c")},
            ),
        )
        result = runner.invoke(
            cli.app,
            ["databases", "add", "emb_db", "--kind", "cdm", "--connection", "c"],
        )
        assert result.exit_code == 0, result.output
        assert "was a GenericDatabaseConfig" in result.output
        config = _load_from_path(isolated_config)
        assert isinstance(config.databases["emb_db"], CDMDatabaseConfig)

    def test_cdm_only_flag_on_generic_kind_fails_instead_of_silently_dropping(
        self, isolated_config
    ):
        """--vocab-connection has no meaning on a generic database. Must be
        rejected, not silently discarded."""
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={"emb": ConnectionConfig(dialect="sqlite")}
            ),
        )
        result = runner.invoke(
            cli.app,
            [
                "databases",
                "add",
                "emb_db",
                "--kind",
                "generic",
                "--connection",
                "emb",
                "--vocab-connection",
                "emb",
            ],
        )
        assert result.exit_code != 0
        assert "vocab_connection" in result.output
        config = _load_from_path(isolated_config)
        assert "emb_db" not in config.databases


class TestDatabasesList:
    def test_empty(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session())
        result = runner.invoke(cli.app, ["databases", "list"])
        assert result.exit_code == 0
        assert "No databases configured" in result.output

    def test_lists_configured_databases(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={"cdm": ConnectionConfig(dialect="sqlite")},
                databases={
                    "cdm_db": CDMDatabaseConfig(connection="cdm", schema_name="omop")
                },
            ),
        )
        result = runner.invoke(cli.app, ["databases", "list"])
        assert result.exit_code == 0
        assert "cdm_db" in result.output
        assert "omop" in result.output

    def test_shows_cdm_only_columns_in_a_mixed_section(
        self, isolated_config, monkeypatch
    ):
        """A GenericDatabaseConfig alongside a CDMDatabaseConfig must not
        hide the CDM-only columns (vocab_connection etc.), which the base
        DatabaseConfig type alone doesn't have."""
        monkeypatch.setenv(
            "COLUMNS", "300"
        )  # avoid rich truncating columns under CliRunner's default width
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={
                    "cdm": ConnectionConfig(dialect="sqlite"),
                    "vocab": ConnectionConfig(dialect="sqlite"),
                    "emb": ConnectionConfig(dialect="sqlite"),
                },
                databases={
                    "cdm_db": CDMDatabaseConfig(
                        connection="cdm", schema_name="omop", vocab_connection="vocab"
                    ),
                    "emb_db": GenericDatabaseConfig(connection="emb"),
                },
            ),
        )
        result = runner.invoke(cli.app, ["databases", "list"])
        assert result.exit_code == 0
        assert "vocab_connection" in result.output
        assert "vocab" in result.output
        assert "emb_db" in result.output


class TestProvidersAdd:
    def test_non_interactive_creates_provider(self, isolated_config):
        result = runner.invoke(
            cli.app,
            [
                "providers",
                "add",
                "local-ollama",
                "--provider",
                "ollama",
                "--base-url",
                "http://localhost:11434",
            ],
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.providers["local-ollama"].provider == "ollama"
        assert config.providers["local-ollama"].base_url == "http://localhost:11434"
        assert config.providers["local-ollama"].api_key is None

    def test_non_interactive_missing_required_field_fails(self, isolated_config):
        result = runner.invoke(
            cli.app,
            [
                "providers",
                "add",
                "local-ollama",
                "--base-url",
                "http://localhost:11434",
            ],
        )
        assert result.exit_code != 0
        assert not isolated_config.exists()

    def test_update_existing_provider(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(providers={"p": ProviderConfig(provider="ollama")}),
        )
        result = runner.invoke(
            cli.app, ["providers", "add", "p", "--api-key", "sk-test"]
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.providers["p"].provider == "ollama"
        assert config.providers["p"].api_key == "sk-test"


class TestProvidersList:
    def test_no_config_file(self, isolated_config):
        result = runner.invoke(cli.app, ["providers", "list"])
        assert result.exit_code != 0

    def test_empty(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session())
        result = runner.invoke(cli.app, ["providers", "list"])
        assert result.exit_code == 0
        assert "No providers configured" in result.output

    def test_lists_configured_providers(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                providers={"p": ProviderConfig(provider="ollama", base_url="http://x")}
            ),
        )
        result = runner.invoke(cli.app, ["providers", "list"])
        assert result.exit_code == 0
        assert "p" in result.output
        assert "ollama" in result.output


class TestModelsAdd:
    def test_non_interactive_creates_model(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(providers={"p": ProviderConfig(provider="ollama")}),
        )
        result = runner.invoke(
            cli.app,
            [
                "models",
                "add",
                "nomic-embed",
                "--provider",
                "p",
                "--model",
                "nomic-embed-text:v1.5",
                "--embeddings",
                "--tool-use",
                "--structured-output",
                "--extended-thinking",
                "--embedding-dim",
                "768",
                "--document-prefix",
                "search_document: ",
                "--query-prefix",
                "search_query: ",
            ],
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        model = config.models["nomic-embed"]
        assert model.provider == "p"
        assert model.model == "nomic-embed-text:v1.5"
        assert model.embedding_dim == 768
        assert model.embeddings is True
        assert model.tool_use is True
        assert model.structured_output is True
        assert model.extended_thinking is True
        assert model.document_prefix == "search_document: "
        assert model.query_prefix == "search_query: "

    def test_unknown_provider_reference_fails(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session())
        result = runner.invoke(
            cli.app,
            [
                "models",
                "add",
                "m",
                "--provider",
                "does-not-exist",
                "--model",
                "local-chat",
            ],
        )
        assert result.exit_code != 0
        config = _load_from_path(isolated_config)
        assert "m" not in config.models

    def test_update_preserves_existing_free_form_configuration(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                providers={"p": ProviderConfig(provider="ollama")},
                models={
                    "m": ModelConfig(
                        provider="p",
                        model="nomic-embed-text",
                        embeddings=True,
                        tool_use=True,
                        structured_output=True,
                        extended_thinking=True,
                        configuration={"max_tokens": 8000},
                    )
                },
            ),
        )
        result = runner.invoke(
            cli.app, ["models", "add", "m", "--embedding-dim", "768"]
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.models["m"].configuration == {"max_tokens": 8000}
        assert config.models["m"].embedding_dim == 768
        assert config.models["m"].embeddings is True
        assert config.models["m"].tool_use is True
        assert config.models["m"].structured_output is True
        assert config.models["m"].extended_thinking is True

    def test_non_embedding_model_rejects_embedding_dim(self, isolated_config):
        """A schema rejection must read as a config error, not a Python one:
        exit 1 and a named complaint, never a pydantic traceback carrying the
        whole input dict and a docs URL."""
        _seed(
            isolated_config,
            StackConfig.for_session(providers={"p": ProviderConfig(provider="ollama")}),
        )
        result = runner.invoke(
            cli.app,
            [
                "models",
                "add",
                "chat",
                "--provider",
                "p",
                "--model",
                "local-chat",
                "--embedding-dim",
                "768",
            ],
        )
        assert result.exit_code == 1
        assert "Invalid ModelConfig" in result.stderr
        assert "embedding_dim requires embeddings=true" in result.stderr
        assert "pydantic.dev" not in result.stderr  # no raw ValidationError rendering
        config = _load_from_path(isolated_config)
        assert "chat" not in config.models

    def test_field_level_validation_error_names_the_flag(self, isolated_config):
        """A field-level failure has a `loc`, so the message can point at the
        exact flag to change rather than the Python field name."""
        _seed(
            isolated_config,
            StackConfig.for_session(providers={"p": ProviderConfig(provider="ollama")}),
        )
        result = runner.invoke(
            cli.app,
            [
                "models",
                "add",
                "m",
                "--provider",
                "p",
                "--model",
                "local-chat",
                "--embeddings",
                "--embedding-dim",
                "not-an-int",
            ],
        )
        assert result.exit_code == 1
        assert "--embedding-dim:" in result.stderr
        config = _load_from_path(isolated_config)
        assert "m" not in config.models

    def test_validation_error_is_reported_interactively_too(
        self, isolated_config, monkeypatch
    ):
        """The same guard covers the prompt path -- answering the confirms in a
        way that contradicts an earlier answer must not raise through the CLI."""
        _seed(
            isolated_config,
            StackConfig.for_session(providers={"p": ProviderConfig(provider="ollama")}),
        )
        answers = iter(["p", "local-chat", "768", "", ""])
        monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: next(answers))
        monkeypatch.setattr(
            cli.typer, "confirm", lambda *a, **k: False
        )  # declines embeddings
        result = runner.invoke(cli.app, ["models", "add", "m"])
        assert result.exit_code == 1
        assert "embedding_dim requires embeddings=true" in result.stderr


class TestResolveRef:
    """_resolve_ref: the generic RefTo-driven reuse-or-create wizard step,
    used both for a package's own RefTo fields and recursively for nested
    ones (e.g. a new database's own connection)."""

    def test_reuse_existing_model(self, monkeypatch):
        monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "m")
        config = StackConfig.for_session(
            providers={"p": ProviderConfig(provider="ollama")},
            models={"m": ModelConfig(provider="p", model="local-chat")},
        )
        result = _resolve_ref(
            "embedding_model_name", "desc", ModelConfig, config, default_name="m"
        )
        assert result == "m"
        assert set(config.models) == {"m"}
        assert set(config.providers) == {"p"}

    def test_create_new_model_and_provider(self, monkeypatch):
        answers = iter(
            [
                "new-model",  # "New model name"
                "new-provider",  # "New provider name"
                "ollama",  # provider: provider
                "",  # provider: base_url
                "",  # provider: api_key
                "local-chat",  # model: model
                "",  # model: embedding_dim
                "",  # model: document_prefix
                "",  # model: query_prefix
            ]
        )
        monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: next(answers))
        monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)
        config = StackConfig.for_session()
        result = _resolve_ref(
            "embedding_model_name",
            "desc",
            ModelConfig,
            config,
            default_name="new-model",
        )
        assert result == "new-model"
        assert config.models["new-model"].provider == "new-provider"
        assert config.models["new-model"].model == "local-chat"
        assert config.providers["new-provider"].provider == "ollama"

    def test_danger_message_test_field_pointed_at_prod_entry(self, monkeypatch, capsys):
        """is_test=True field named at an existing non-test entry: the
        DANGER message must say the entry isn't test_only."""
        monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "prod_db")
        config = StackConfig.for_session(
            connections={
                "prod": ConnectionConfig(dialect="sqlite", database_name=":memory:")
            },
            databases={"prod_db": CDMDatabaseConfig(connection="prod")},
        )
        with pytest.raises(typer.Exit):
            _resolve_ref(
                "test_cdm_db",
                "desc",
                CDMDatabaseConfig,
                config,
                default_name="prod_db",
                is_test=True,
            )
        err = capsys.readouterr().err
        assert "prod_db" in err
        assert "is not marked test_only=true" in err

    def test_danger_message_prod_field_pointed_at_test_entry(self, monkeypatch, capsys):
        """is_test=False field named at an existing test-only entry: the
        DANGER message must say the opposite of the test_field case, not
        the same hardcoded text."""
        monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: "test_db")
        config = StackConfig.for_session(
            connections={
                "test_conn": ConnectionConfig(
                    dialect="sqlite", database_name=":memory:", test_only=True
                )
            },
            databases={"test_db": CDMDatabaseConfig(connection="test_conn")},
        )
        with pytest.raises(typer.Exit):
            _resolve_ref(
                "cdm_db",
                "desc",
                CDMDatabaseConfig,
                config,
                default_name="test_db",
                is_test=False,
            )
        err = capsys.readouterr().err
        assert "test_db" in err
        assert "is marked test_only=true" in err
        assert "is not marked test_only=true" not in err


class DemoConfig(PackageConfigBase):
    """Stand-in package: a required database, an opt-in test database, an
    opt-in non-test database, and a plain extra field. Exercises
    PackageConfigBase.run_configure end to end."""

    tool_name: ClassVar[str] = "demo_tool"
    cdm_db: Annotated[str, RefTo(CDMDatabaseConfig)] = "cdm_db"
    test_cdm_db: Annotated[str | None, RefTo(CDMDatabaseConfig, is_test=True)] = None
    secondary_db: Annotated[str | None, RefTo(CDMDatabaseConfig)] = None
    backend: str = "default_backend"


def _echo_default(text, default="", **kwargs):
    """typer.prompt stand-in: always accept whatever default was offered."""
    return default


class TestRunConfigurePackage:
    def test_non_interactive_uses_given_names(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={
                    "db": ConnectionConfig(dialect="sqlite", database_name=":memory:")
                },
                databases={"cdm_db": CDMDatabaseConfig(connection="db")},
            ),
        )
        DemoConfig.run_configure(
            {"cdm_db": "cdm_db", "backend": "custom"}, interactive=False
        )
        config = _load_from_path(isolated_config)
        assert config.tools["demo_tool"]["cdm_db"] == "cdm_db"
        assert config.tools["demo_tool"]["backend"] == "custom"

    def test_invalid_package_values_do_not_change_file(self, isolated_config, capsys):
        class PortConfig(PackageConfigBase):
            tool_name: ClassVar[str] = "port_tool"
            port: int = Field(default=8000, ge=1, le=65535)

        _seed(
            isolated_config,
            StackConfig.for_session(tools={"port_tool": {"port": 8000}}),
        )
        original = isolated_config.read_bytes()

        with pytest.raises(typer.Exit):
            PortConfig.run_configure({"port": "999999"}, interactive=False)

        captured = capsys.readouterr()
        assert "Invalid [tools.port_tool]" in captured.err
        assert "Saved [tools.port_tool]" not in captured.out
        assert isolated_config.read_bytes() == original

    def test_validated_package_values_are_normalized_before_save(self, isolated_config):
        class PortConfig(PackageConfigBase):
            tool_name: ClassVar[str] = "port_tool"
            port: int = Field(default=8000, ge=1, le=65535)

        _seed(isolated_config, StackConfig.for_session())

        PortConfig.run_configure({"port": "9000"}, interactive=False)

        config = _load_from_path(isolated_config)
        assert config.tools["port_tool"]["port"] == 9000
        assert isinstance(config.tools["port_tool"]["port"], int)

    def test_interactive_creates_database_and_connection_recursively(
        self, isolated_config, monkeypatch
    ):
        monkeypatch.setattr(cli.typer, "prompt", _echo_default)
        monkeypatch.setattr(
            cli.typer, "confirm", lambda *a, **k: False
        )  # decline both optional databases
        _seed(isolated_config, StackConfig.for_session())

        DemoConfig.run_configure({}, interactive=True)

        config = _load_from_path(isolated_config)
        assert config.tools["demo_tool"]["cdm_db"] == "cdm_db"
        assert "cdm_db" in config.databases
        assert config.databases["cdm_db"].connection in config.connections
        assert config.databases["cdm_db"].schema_name == "omop"
        # vocab_connection is optional, so it is never auto-created
        assert config.databases["cdm_db"].vocab_connection is None
        # both optional databases were declined, so neither was written
        assert "test_cdm_db" not in config.tools["demo_tool"]
        assert "secondary_db" not in config.tools["demo_tool"]

    def test_interactive_declines_optional_non_test_database(
        self, isolated_config, monkeypatch
    ):
        """A plain Optional RefTo field (is_test=False) gets the same
        'Configure this?' skip prompt as a test field; declining leaves it
        unset rather than resolving/creating an entry."""
        monkeypatch.setattr(cli.typer, "prompt", _echo_default)
        monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: False)
        _seed(isolated_config, StackConfig.for_session())

        DemoConfig.run_configure({}, interactive=True)

        config = _load_from_path(isolated_config)
        assert "secondary_db" not in config.tools["demo_tool"]

    def test_interactive_opts_into_optional_non_test_database(
        self, isolated_config, monkeypatch
    ):
        """Accepting the skip prompt for a non-test Optional RefTo field
        resolves it through the normal RefTo flow, with no test_only
        requirement, unlike the test-database path: it happily reuses
        cdm_db (created earlier in the same run) via the offered default."""
        monkeypatch.setattr(cli.typer, "prompt", _echo_default)

        def confirm(text, *a, **k):
            return text == "Configure secondary_db?"

        monkeypatch.setattr(cli.typer, "confirm", confirm)
        _seed(isolated_config, StackConfig.for_session())

        DemoConfig.run_configure({}, interactive=True)

        config = _load_from_path(isolated_config)
        assert config.tools["demo_tool"]["secondary_db"] == "cdm_db"
        conn_name = config.databases["cdm_db"].connection
        assert config.connections[conn_name].test_only is False

    def test_interactive_opts_into_test_database(self, isolated_config, monkeypatch):
        # Give the second-ever host prompt (the test database's) a distinct
        # value, so it doesn't collide with the production one. Both would
        # otherwise get identical blank host/port/database_name and trip the
        # DANGER collision check, correctly, just not what this test is about.
        seen: dict[str, int] = {}

        def prompt(text, default="", **kwargs):
            seen[text] = seen.get(text, 0) + 1
            if text.startswith("Hostname") and seen[text] == 2:
                return "test-host"
            return default

        monkeypatch.setattr(cli.typer, "prompt", prompt)
        # accept the test database, decline the unrelated non-test optional one
        monkeypatch.setattr(
            cli.typer, "confirm", lambda text, *a, **k: text == "Configure test_cdm_db?"
        )
        _seed(isolated_config, StackConfig.for_session())

        DemoConfig.run_configure({}, interactive=True)

        config = _load_from_path(isolated_config)
        test_name = config.tools["demo_tool"]["test_cdm_db"]
        assert test_name in config.databases
        test_conn_name = config.databases[test_name].connection
        assert config.connections[test_conn_name].test_only is True

    def test_interactive_reconfigure_reprompts_with_stored_default(
        self, isolated_config, monkeypatch
    ):
        """A field that's already configured must be offered for change, not
        silently reused. The prompt default should be the stored value,
        and a different answer should actually change it."""
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={
                    "db": ConnectionConfig(dialect="sqlite", database_name=":memory:")
                },
                databases={"cdm_db": CDMDatabaseConfig(connection="db")},
            ),
        )
        DemoConfig.run_configure(
            {"cdm_db": "cdm_db", "backend": "first_value"}, interactive=False
        )

        seen_defaults: dict[str, str] = {}

        def prompt(text, default="", **kwargs):
            seen_defaults[text] = default
            return "second_value" if text == "backend" else default

        monkeypatch.setattr(cli.typer, "prompt", prompt)
        monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: False)

        DemoConfig.run_configure({}, interactive=True)

        assert seen_defaults["backend"] == "first_value"
        config = _load_from_path(isolated_config)
        assert config.tools["demo_tool"]["backend"] == "second_value"

    def test_interactive_reconfigure_reprompts_refto_field_with_stored_default(
        self, isolated_config, monkeypatch
    ):
        """Same as above but for a RefTo field: _resolve_ref must be offered
        the stored database name as its suggested default, not skipped."""
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={
                    "db": ConnectionConfig(dialect="sqlite", database_name=":memory:")
                },
                databases={"cdm_db": CDMDatabaseConfig(connection="db")},
            ),
        )
        DemoConfig.run_configure(
            {"cdm_db": "cdm_db", "backend": "x"}, interactive=False
        )

        seen_defaults: dict[str, str] = {}

        def prompt(text, default="", **kwargs):
            seen_defaults[text] = default
            return default

        monkeypatch.setattr(cli.typer, "prompt", prompt)
        monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: False)

        DemoConfig.run_configure({}, interactive=True)

        assert (
            seen_defaults["  Point to an existing entry, or 'new' to create one"]
            == "cdm_db"
        )
        config = _load_from_path(isolated_config)
        assert config.tools["demo_tool"]["cdm_db"] == "cdm_db"

    def test_non_interactive_one_shot_creates_database_and_connection(
        self, isolated_config
    ):
        """--set-style nested flags create the whole reference chain (a new
        connection, and the database pointing at it) in one non-interactive
        call, restoring the old one-shot Docker Compose workflow."""
        _seed(isolated_config, StackConfig.for_session())

        DemoConfig.run_configure(
            {
                "backend": "custom",
                "cdm_db": {
                    "connection": {
                        "dialect": "sqlite",
                        "database_name": ":memory:",
                    },
                    "schema_name": "omop",
                },
            },
            interactive=False,
        )

        config = _load_from_path(isolated_config)
        cdm_db_name = config.tools["demo_tool"]["cdm_db"]
        assert cdm_db_name in config.databases
        conn_name = config.databases[cdm_db_name].connection
        assert config.connections[conn_name].dialect == "sqlite"
        assert config.databases[cdm_db_name].schema_name == "omop"

    def test_non_interactive_one_shot_missing_required_nested_field_fails(
        self, isolated_config
    ):
        _seed(isolated_config, StackConfig.for_session())

        with pytest.raises(typer.Exit):
            DemoConfig.run_configure(
                {"cdm_db": {"schema_name": "omop"}},  # missing connection.dialect etc.
                interactive=False,
            )


class TestParseSetFlags:
    def test_flat_key(self):
        assert cli._parse_set_flags(("backend=custom",)) == {"backend": "custom"}

    def test_nested_key(self):
        assert cli._parse_set_flags(("cdm_db.dialect=sqlite", "cdm_db.host=db")) == {
            "cdm_db": {"dialect": "sqlite", "host": "db"}
        }

    def test_deeply_nested_key(self):
        assert cli._parse_set_flags(("cdm_db.connection.dialect=sqlite",)) == {
            "cdm_db": {"connection": {"dialect": "sqlite"}}
        }

    def test_missing_equals_raises(self):
        with pytest.raises(typer.BadParameter):
            cli._parse_set_flags(("cdm_db.dialect",))

    def test_path_conflict_raises(self):
        with pytest.raises(typer.BadParameter):
            cli._parse_set_flags(("cdm_db=name", "cdm_db.dialect=sqlite"))


class TestConfigureSetFlag:
    """Full CLI dispatch (not just PackageConfigBase.run_configure directly)
    for the --set flag, via a faked entry point. Proves --set is actually
    wired through Click/Typer parsing, not just the underlying resolution."""

    def test_set_flag_creates_nested_entry_via_full_cli(
        self, isolated_config, monkeypatch
    ):
        class FakeEP:
            name = "demo_tool"

            def load(self):
                return DemoConfig

        monkeypatch.setattr(
            cli,
            "entry_points",
            lambda group=None: [FakeEP()] if group == cli.ENTRY_POINT_GROUP else [],
        )
        result = runner.invoke(
            cli.app,
            [
                "configure",
                "demo_tool",
                "--backend",
                "custom",
                "--set",
                "cdm_db.connection.dialect=sqlite",
                "--set",
                "cdm_db.connection.database_name=:memory:",
                "--set",
                "cdm_db.schema_name=omop",
            ],
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.tools["demo_tool"]["backend"] == "custom"
        cdm_db_name = config.tools["demo_tool"]["cdm_db"]
        assert cdm_db_name in config.databases
        conn_name = config.databases[cdm_db_name].connection
        assert config.connections[conn_name].dialect == "sqlite"

    def test_set_flag_clashing_with_same_field_flag_fails(
        self, isolated_config, monkeypatch
    ):
        """--cdm-db and --set cdm_db.* both target the cdm_db field: must be
        rejected, not have --set silently win and edit/create the wrong
        (default-named) entry while the flag's chosen entry is untouched."""
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={
                    "prod": ConnectionConfig(dialect="sqlite", database_name=":memory:")
                },
                databases={
                    "cdm_db_prod": CDMDatabaseConfig(
                        connection="prod", schema_name="prod_omop"
                    )
                },
            ),
        )

        class FakeEP:
            name = "demo_tool"

            def load(self):
                return DemoConfig

        monkeypatch.setattr(
            cli,
            "entry_points",
            lambda group=None: [FakeEP()] if group == cli.ENTRY_POINT_GROUP else [],
        )
        result = runner.invoke(
            cli.app,
            [
                "configure",
                "demo_tool",
                "--cdm-db",
                "cdm_db_prod",
                "--set",
                "cdm_db.schema_name=analytics",
            ],
        )
        assert result.exit_code != 0
        assert "cdm_db" in result.output
        config = _load_from_path(isolated_config)
        assert config.databases["cdm_db_prod"].schema_name == "prod_omop"
        assert "cdm_db" not in config.databases
        assert "demo_tool" not in config.tools

    def test_set_flag_typo_in_subfield_fails_instead_of_silent_no_op(
        self, isolated_config, monkeypatch
    ):
        """cdm_db.shema_name (typo for schema_name) must be rejected, not
        silently dropped while the rest of the entry still saves."""

        class FakeEP:
            name = "demo_tool"

            def load(self):
                return DemoConfig

        monkeypatch.setattr(
            cli,
            "entry_points",
            lambda group=None: [FakeEP()] if group == cli.ENTRY_POINT_GROUP else [],
        )
        result = runner.invoke(
            cli.app,
            [
                "configure",
                "demo_tool",
                "--backend",
                "custom",
                "--set",
                "cdm_db.connection.dialect=sqlite",
                "--set",
                "cdm_db.connection.database_name=:memory:",
                "--set",
                "cdm_db.shema_name=omop",
            ],
        )
        assert result.exit_code != 0
        assert "shema_name" in result.output


class TestModelsList:
    def test_empty(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session())
        result = runner.invoke(cli.app, ["models", "list"])
        assert result.exit_code == 0
        assert "No models configured" in result.output

    def test_lists_configured_models(self, isolated_config, monkeypatch):
        monkeypatch.setenv(
            "COLUMNS", "300"
        )  # avoid rich truncating columns under CliRunner's default width
        _seed(
            isolated_config,
            StackConfig.for_session(
                providers={"p": ProviderConfig(provider="ollama")},
                models={
                    "nomic-embed": ModelConfig(
                        provider="p",
                        model="nomic-embed-text",
                        embedding_dim=768,
                        embeddings=True,
                        document_prefix="search_document: ",
                    )
                },
            ),
        )
        result = runner.invoke(cli.app, ["models", "list"])
        assert result.exit_code == 0
        assert "nomic-embed" in result.output
        assert "768" in result.output


class TestVectorStoresAdd:
    def test_non_interactive_creates_vector_store(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={
                    "emb": ConnectionConfig(dialect="sqlite", database_name=":memory:")
                },
                databases={"emb_db": GenericDatabaseConfig(connection="emb")},
            ),
        )
        result = runner.invoke(
            cli.app,
            [
                "vector-stores",
                "add",
                "vs",
                "--backend-type",
                "pgvector",
                "--database",
                "emb_db",
            ],
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.vector_stores["vs"].backend_type == "pgvector"
        assert config.vector_stores["vs"].database == "emb_db"

    def test_non_interactive_missing_required_field_fails(self, isolated_config):
        result = runner.invoke(
            cli.app, ["vector-stores", "add", "vs", "--backend-type", "pgvector"]
        )
        assert result.exit_code != 0
        assert not isolated_config.exists()

    def test_unknown_database_reference_fails(self, isolated_config):
        result = runner.invoke(
            cli.app,
            [
                "vector-stores",
                "add",
                "vs",
                "--backend-type",
                "pgvector",
                "--database",
                "does-not-exist",
            ],
        )
        assert result.exit_code != 0
        assert not isolated_config.exists()

    def test_update_existing_vector_store(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={
                    "emb": ConnectionConfig(dialect="sqlite", database_name=":memory:")
                },
                databases={"emb_db": GenericDatabaseConfig(connection="emb")},
                vector_stores={
                    "vs": VectorStoreConfig(backend_type="sqlitevec", database="emb_db")
                },
            ),
        )
        result = runner.invoke(
            cli.app, ["vector-stores", "add", "vs", "--backend-type", "pgvector"]
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.vector_stores["vs"].backend_type == "pgvector"
        assert config.vector_stores["vs"].database == "emb_db"


class TestVectorStoresList:
    def test_no_config_file(self, isolated_config):
        result = runner.invoke(cli.app, ["vector-stores", "list"])
        assert result.exit_code != 0

    def test_empty(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session())
        result = runner.invoke(cli.app, ["vector-stores", "list"])
        assert result.exit_code == 0
        assert "No vector_stores configured" in result.output

    def test_lists_configured_vector_stores(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                connections={
                    "emb": ConnectionConfig(dialect="sqlite", database_name=":memory:")
                },
                databases={"emb_db": GenericDatabaseConfig(connection="emb")},
                vector_stores={
                    "vs": VectorStoreConfig(backend_type="pgvector", database="emb_db")
                },
            ),
        )
        result = runner.invoke(cli.app, ["vector-stores", "list"])
        assert result.exit_code == 0
        assert "vs" in result.output
        assert "pgvector" in result.output
