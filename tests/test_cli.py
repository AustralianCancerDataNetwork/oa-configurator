"""Tests for cli.py: providers/models add/list commands.

Rule: no test reads from or writes to ~/.config/omop/. Each test redirects
cli.py's CONFIG_PATH/load_stack_config/save_stack_config to a tmp_path file.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import oa_configurator.cli as cli
from oa_configurator.io import save_stack_config as _real_save_stack_config
from oa_configurator.loader import _load_from_path
from oa_configurator.models import ModelConfig, ProviderConfig, StackConfig

runner = CliRunner()


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect cli.py's config path/load/save to an isolated tmp_path file."""
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "load_stack_config", lambda: _load_from_path(config_path))
    monkeypatch.setattr(cli, "save_stack_config", lambda config: _real_save_stack_config(config, path=config_path))
    return config_path


def _seed(config_path, config: StackConfig) -> None:
    _real_save_stack_config(config, path=config_path)


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
