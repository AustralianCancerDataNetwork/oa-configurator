"""Tests for cli.py: connections/databases/providers/models add/list commands."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from typing import Annotated, ClassVar

import oa_configurator.cli as cli
from oa_configurator.io import save_stack_config as _real_save_stack_config
from oa_configurator.loader import _load_from_path
from oa_configurator.stack_config import ConnectionConfig, DatabaseConfig, ModelConfig, ProviderConfig, RefTo, StackConfig
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
            ["connections", "add", "cdm", "--dialect", "sqlite", "--database-name", ":memory:"],
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.connections["cdm"].dialect == "sqlite"
        assert config.connections["cdm"].database_name == ":memory:"

    def test_non_interactive_missing_required_field_fails(self, isolated_config):
        result = runner.invoke(cli.app, ["connections", "add", "cdm", "--host", "localhost"])
        assert result.exit_code != 0
        assert not isolated_config.exists()

    def test_update_existing_connection(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session(connections={"cdm": ConnectionConfig(dialect="sqlite")}))
        result = runner.invoke(cli.app, ["connections", "add", "cdm", "--host", "otherhost"])
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.connections["cdm"].dialect == "sqlite"
        assert config.connections["cdm"].host == "otherhost"


class TestConnectionsList:
    def test_empty(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session())
        result = runner.invoke(cli.app, ["connections", "list"])
        assert result.exit_code == 0
        assert "No connections configured" in result.output

    def test_lists_configured_connections(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session(connections={"cdm": ConnectionConfig(dialect="sqlite")}))
        result = runner.invoke(cli.app, ["connections", "list"])
        assert result.exit_code == 0
        assert "cdm" in result.output
        assert "sqlite" in result.output


class TestDatabasesAdd:
    def test_non_interactive_creates_database(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session(connections={"cdm": ConnectionConfig(dialect="sqlite")}))
        result = runner.invoke(cli.app, ["databases", "add", "cdm_db", "--connection", "cdm", "--cdm-schema", "omop"])
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.databases["cdm_db"].connection == "cdm"
        assert config.databases["cdm_db"].cdm_schema == "omop"

    def test_unknown_connection_reference_fails(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session())
        result = runner.invoke(cli.app, ["databases", "add", "cdm_db", "--connection", "does-not-exist"])
        assert result.exit_code != 0
        config = _load_from_path(isolated_config)
        assert "cdm_db" not in config.databases


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
                databases={"cdm_db": DatabaseConfig(connection="cdm", cdm_schema="omop")},
            ),
        )
        result = runner.invoke(cli.app, ["databases", "list"])
        assert result.exit_code == 0
        assert "cdm_db" in result.output
        assert "omop" in result.output


class TestProvidersAdd:
    def test_non_interactive_creates_provider(self, isolated_config):
        result = runner.invoke(
            cli.app,
            ["providers", "add", "local-ollama", "--provider", "ollama", "--base-url", "http://localhost:11434"],
        )
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.providers["local-ollama"].provider == "ollama"
        assert config.providers["local-ollama"].base_url == "http://localhost:11434"
        assert config.providers["local-ollama"].api_key is None

    def test_non_interactive_missing_required_field_fails(self, isolated_config):
        result = runner.invoke(cli.app, ["providers", "add", "local-ollama", "--base-url", "http://localhost:11434"])
        assert result.exit_code != 0
        assert not isolated_config.exists()

    def test_update_existing_provider(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session(providers={"p": ProviderConfig(provider="ollama")}))
        result = runner.invoke(cli.app, ["providers", "add", "p", "--api-key", "sk-test"])
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
            StackConfig.for_session(providers={"p": ProviderConfig(provider="ollama", base_url="http://x")}),
        )
        result = runner.invoke(cli.app, ["providers", "list"])
        assert result.exit_code == 0
        assert "p" in result.output
        assert "ollama" in result.output


class TestModelsAdd:
    def test_non_interactive_creates_model(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session(providers={"p": ProviderConfig(provider="ollama")}))
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
        assert model.document_prefix == "search_document: "
        assert model.query_prefix == "search_query: "

    def test_unknown_provider_reference_fails(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session())
        result = runner.invoke(
            cli.app,
            ["models", "add", "m", "--provider", "does-not-exist", "--model", "local-chat"],
        )
        assert result.exit_code != 0
        config = _load_from_path(isolated_config)
        assert "m" not in config.models

    def test_update_preserves_existing_free_form_configuration(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                providers={"p": ProviderConfig(provider="ollama")},
                models={"m": ModelConfig(provider="p", model="local-chat", configuration={"max_tokens": 8000})},
            ),
        )
        result = runner.invoke(cli.app, ["models", "add", "m", "--embedding-dim", "768"])
        assert result.exit_code == 0, result.output
        config = _load_from_path(isolated_config)
        assert config.models["m"].configuration == {"max_tokens": 8000}
        assert config.models["m"].embedding_dim == 768


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
        result = _resolve_ref("embedding_model_name", "desc", ModelConfig, config, default_name="m")
        assert result == "m"
        assert set(config.models) == {"m"}
        assert set(config.providers) == {"p"}

    def test_create_new_model_and_provider(self, monkeypatch):
        answers = iter([
            "new-model",     # "New model name"
            "new-provider",  # "New provider name"
            "ollama",        # provider: provider
            "",              # provider: base_url
            "",              # provider: api_key
            "local-chat",    # model: model
            "",              # model: embedding_dim
            "",              # model: document_prefix
            "",              # model: query_prefix
        ])
        monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: next(answers))
        config = StackConfig.for_session()
        result = _resolve_ref("embedding_model_name", "desc", ModelConfig, config, default_name="new-model")
        assert result == "new-model"
        assert config.models["new-model"].provider == "new-provider"
        assert config.models["new-model"].model == "local-chat"
        assert config.providers["new-provider"].provider == "ollama"


class DemoConfig(PackageConfigBase):
    """Stand-in package: a required database, an opt-in test database, and
    a plain extra field -- exercises PackageConfigBase.run_configure end to end."""

    tool_name: ClassVar[str] = "demo_tool"
    cdm_db: Annotated[str, RefTo(DatabaseConfig)] = "cdm_db"
    test_cdm_db: Annotated[str | None, RefTo(DatabaseConfig)] = None
    backend: str = "default_backend"


def _echo_default(text, default="", **kwargs):
    """typer.prompt stand-in: always accept whatever default was offered."""
    return default


class TestRunConfigurePackage:
    def test_non_interactive_uses_given_names(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session(
            connections={"db": ConnectionConfig(dialect="sqlite", database_name=":memory:")},
            databases={"cdm_db": DatabaseConfig(connection="db")},
        ))
        DemoConfig.run_configure({"cdm_db": "cdm_db", "backend": "custom"}, interactive=False)
        config = _load_from_path(isolated_config)
        assert config.tools["demo_tool"]["cdm_db"] == "cdm_db"
        assert config.tools["demo_tool"]["backend"] == "custom"

    def test_interactive_creates_database_and_connection_recursively(self, isolated_config, monkeypatch):
        monkeypatch.setattr(cli.typer, "prompt", _echo_default)
        monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: False)  # decline the test database
        _seed(isolated_config, StackConfig.for_session())

        DemoConfig.run_configure({}, interactive=True)

        config = _load_from_path(isolated_config)
        assert config.tools["demo_tool"]["cdm_db"] == "cdm_db"
        assert "cdm_db" in config.databases
        assert config.databases["cdm_db"].connection in config.connections
        assert config.databases["cdm_db"].cdm_schema == "omop"
        # vocab_connection is optional -- never auto-created
        assert config.databases["cdm_db"].vocab_connection is None
        # test database was declined -- not written at all
        assert "test_cdm_db" not in config.tools["demo_tool"]

    def test_interactive_opts_into_test_database(self, isolated_config, monkeypatch):
        # Give the second-ever host prompt (the test database's) a distinct
        # value, so it doesn't collide with the production one -- both would
        # otherwise get identical blank host/port/database_name and trip the
        # DANGER collision check, correctly, just not what this test is about.
        seen: dict[str, int] = {}

        def prompt(text, default="", **kwargs):
            seen[text] = seen.get(text, 0) + 1
            if text.startswith("Hostname") and seen[text] == 2:
                return "test-host"
            return default

        monkeypatch.setattr(cli.typer, "prompt", prompt)
        monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)  # accept the test database
        _seed(isolated_config, StackConfig.for_session())

        DemoConfig.run_configure({}, interactive=True)

        config = _load_from_path(isolated_config)
        test_name = config.tools["demo_tool"]["test_cdm_db"]
        assert test_name in config.databases
        test_conn_name = config.databases[test_name].connection
        assert config.connections[test_conn_name].test_only is True


class TestModelsList:
    def test_empty(self, isolated_config):
        _seed(isolated_config, StackConfig.for_session())
        result = runner.invoke(cli.app, ["models", "list"])
        assert result.exit_code == 0
        assert "No models configured" in result.output

    def test_lists_configured_models(self, isolated_config):
        _seed(
            isolated_config,
            StackConfig.for_session(
                providers={"p": ProviderConfig(provider="ollama")},
                models={
                    "nomic-embed": ModelConfig(
                        provider="p",
                        model="nomic-embed-text",
                        embedding_dim=768,
                        document_prefix="search_document: ",
                    )
                },
            ),
        )
        result = runner.invoke(cli.app, ["models", "list"])
        assert result.exit_code == 0
        assert "nomic-embed" in result.output
        assert "768" in result.output
