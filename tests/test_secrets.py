"""Tests for the stack's secret-handling primitives.

Five packages previously kept private word lists of "secret-looking" field
names, and the drift between two of them leaked a credential. The replacement
is a single rule -- a field is secret because it says so -- so these tests are
mostly about proving that nothing here decides sensitivity by name, and that
the display and validation paths built on the marker hold up.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

import pytest
from pydantic import BaseModel, ValidationError

from oa_configurator import (
    ConnectionConfig,
    ProviderConfig,
    Secret,
    Sensitive,
    is_sensitive,
    safe_endpoint,
)


class _Fields(BaseModel):
    """Covers the marker present/absent axis against a suspicious name."""

    marked: Secret = None
    spelled_out: Annotated[str | None, Sensitive()] = None
    password: str | None = None  # secret-looking name, no marker
    sort_key: str | None = None  # innocent name a word list would flag
    host: str | None = None


def _field(name: str):
    return _Fields.model_fields[name]


def _query(url: str) -> list[tuple[str, str]]:
    """Raw ``(key, value)`` pairs, so assertions see exactly what is rendered."""
    return [
        (param.partition("=")[0], param.partition("=")[2])
        for param in urlsplit(url).query.split("&")
        if param
    ]


class TestSecretAlias:
    def test_is_the_spelled_out_annotation(self):
        assert Secret == Annotated[str | None, Sensitive()]

    def test_carries_the_marker_through_a_model(self):
        assert is_sensitive(_field("marked")) is True

    def test_matches_the_spelled_out_form(self):
        assert _field("marked").metadata == _field("spelled_out").metadata

    def test_declared_fields_use_it(self):
        """The two secrets oa-configurator owns are the worked example."""
        assert is_sensitive(ProviderConfig.model_fields["api_key"]) is True
        assert is_sensitive(ConnectionConfig.model_fields["password"]) is True


class TestIsSensitive:
    """The stack's only runtime sensitivity predicate: marker, nothing else."""

    def test_marked_field(self):
        assert is_sensitive(_field("marked")) is True

    def test_secret_looking_name_without_a_marker_is_not_sensitive(self):
        """A missing marker is a bug to catch at test time, not to guess around.

        Guessing here is what produced the divergent word lists this module
        replaced; `assert_no_sensitive_values_leak` is the safety net instead.
        """
        assert is_sensitive(_field("password")) is False

    def test_innocent_name_is_never_flagged(self):
        assert is_sensitive(_field("sort_key")) is False

    def test_plain_field(self):
        assert is_sensitive(_field("host")) is False

    def test_unmarked_declared_fields(self):
        assert is_sensitive(ProviderConfig.model_fields["base_url"]) is False
        assert is_sensitive(ConnectionConfig.model_fields["user"]) is False


class TestSafeEndpoint:
    def test_none_passes_through(self):
        assert safe_endpoint(None) is None

    def test_plain_url_unchanged(self):
        assert (
            safe_endpoint("https://api.example.org/v1") == "https://api.example.org/v1"
        )

    def test_port_and_path_preserved(self):
        assert safe_endpoint("http://localhost:11434/v1") == "http://localhost:11434/v1"

    def test_password_masked_username_kept(self):
        """Matches `safe_url`: the account name is diagnostic, not a secret."""
        assert safe_endpoint("https://user:pw@host/v1") == "https://user:***@host/v1"

    def test_username_only_userinfo_kept(self):
        assert safe_endpoint("postgresql://omop_prod@db.hospital.org:5432/omop") == (
            "postgresql://omop_prod@db.hospital.org:5432/omop"
        )

    def test_userinfo_masking_keeps_the_port(self):
        assert safe_endpoint("https://user:pw@host:8443/v1") == (
            "https://user:***@host:8443/v1"
        )

    def test_ipv6_host_survives(self):
        assert safe_endpoint("https://[::1]:8443/v1") == "https://[::1]:8443/v1"

    def test_at_sign_in_path_is_not_userinfo(self):
        assert safe_endpoint("https://host/models/org@v1") == (
            "https://host/models/org@v1"
        )

    def test_every_query_value_masked_and_every_key_kept(self):
        """No key is judged: the operator sees which parameters are set, and
        no value at all. Azure OpenAI's `api-version` is why the query string
        cannot simply be dropped."""
        assert _query(
            safe_endpoint("https://host/v1?api-version=2024-02-01&api_key=sk-x")
        ) == [("api-version", "***"), ("api_key", "***")]

    def test_innocuous_value_is_masked_too(self):
        assert safe_endpoint("https://host/v1?model=gpt") == "https://host/v1?model=***"

    def test_mask_is_not_percent_encoded(self):
        """The result is display output: it must read as ``***``, not ``%2A%2A%2A``."""
        assert safe_endpoint("https://host/v1?api_key=abc") == (
            "https://host/v1?api_key=***"
        )

    def test_blank_value_still_masked(self):
        assert safe_endpoint("https://host/v1?token=") == "https://host/v1?token=***"

    def test_valueless_flag_kept(self):
        assert safe_endpoint("https://host/v1?stream") == "https://host/v1?stream"

    def test_key_spelling_preserved_verbatim(self):
        assert safe_endpoint("https://host/v1?X-Api-Key=leak") == (
            "https://host/v1?X-Api-Key=***"
        )

    def test_userinfo_and_query_together(self):
        redacted = safe_endpoint("https://user:pw@host:8443/v1/chat?api_key=abc&model=gpt")
        assert "pw" not in urlsplit(redacted).netloc
        assert "abc" not in redacted
        assert redacted == "https://user:***@host:8443/v1/chat?api_key=***&model=***"

    def test_fragment_is_masked_whole(self):
        """The OAuth implicit flow delivers access tokens in the fragment.

        Masked rather than dropped: a fragment has no guaranteed ``key=value``
        structure to mask value-by-value, and dropping it silently would hide
        that an operator put something in a ``base_url`` that does not belong
        there.
        """
        assert safe_endpoint("https://host/v1#access_token=abc") == (
            "https://host/v1#***"
        )

    def test_opaque_fragment_is_masked_too(self):
        """No fragment is judged on its shape, for the same reason no query key is."""
        assert safe_endpoint("https://host/v1#section-3") == "https://host/v1#***"

    def test_empty_fragment_adds_no_mask(self):
        """A bare trailing ``#`` holds nothing, so it renders as nothing."""
        assert safe_endpoint("https://host/v1#") == "https://host/v1"

    def test_fragment_masked_alongside_query_and_userinfo(self):
        redacted = safe_endpoint("https://user:pw@host/v1?api_key=abc#token=xyz")
        assert redacted == "https://user:***@host/v1?api_key=***#***"
        assert "pw" not in redacted
        assert "abc" not in redacted
        assert "xyz" not in redacted

    def test_unparseable_url_is_withheld_entirely(self):
        """Nothing structural can be trusted, so nothing is echoed back."""
        assert safe_endpoint("https://[::1/v1?api_key=abc") == "***"

    def test_non_url_string_has_nothing_to_mask(self):
        assert safe_endpoint("ollama") == "ollama"


class TestProviderBaseUrlValidation:
    def test_plain_url_accepted(self):
        assert (
            ProviderConfig(provider="vllm", base_url="https://host/v1").base_url
            == "https://host/v1"
        )

    def test_unset_accepted(self):
        assert ProviderConfig(provider="ollama").base_url is None

    def test_query_parameters_accepted(self):
        """Azure-style endpoints are legitimate, and no parameter name is judged."""
        url = "https://x.openai.azure.com/openai/deployments/gpt?api-version=2024-02-01"
        assert ProviderConfig(provider="openai", base_url=url).base_url == url

    def test_userinfo_with_password_rejected(self):
        with pytest.raises(ValidationError, match="userinfo"):
            ProviderConfig(provider="vllm", base_url="https://user:pw@host/v1")

    def test_username_only_userinfo_rejected(self):
        with pytest.raises(ValidationError, match="userinfo"):
            ProviderConfig(provider="vllm", base_url="https://user@host/v1")

    def test_rejection_names_api_key(self):
        with pytest.raises(ValidationError, match="api_key"):
            ProviderConfig(provider="vllm", base_url="https://user:pw@host/v1")

    def test_unparseable_url_rejected(self):
        with pytest.raises(ValidationError, match="not a valid URL"):
            ProviderConfig(provider="vllm", base_url="https://[::1/v1")

    def test_secret_in_a_query_parameter_is_not_rejected(self):
        """Detecting this would need a word list. `safe_endpoint` covers the
        display side without one, so validation does not guess here."""
        url = "https://host/v1?passwd=x"
        assert ProviderConfig(provider="vllm", base_url=url).base_url == url
        assert safe_endpoint(url) == "https://host/v1?passwd=***"
