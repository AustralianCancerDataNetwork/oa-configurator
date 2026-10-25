"""Tests for the deprecated oa_configurator.refs import path."""

from __future__ import annotations

import pytest

from oa_configurator import references, sensitive


@pytest.mark.parametrize(
    ("name", "module"),
    [
        ("RefTo", references),
        ("Secret", sensitive),
        ("Sensitive", sensitive),
        ("SecretSafeBaseModel", sensitive),
        ("is_sensitive", sensitive),
        ("safe_endpoint", sensitive),
        ("MASK", sensitive),
    ],
)
def test_moved_name_resolves_to_its_new_home_with_a_warning(name, module):
    from oa_configurator import refs

    with pytest.warns(
        DeprecationWarning, match=f"oa_configurator.{module.__name__.split('.')[-1]}"
    ):
        value = getattr(refs, name)

    assert value is getattr(module, name)


def test_unknown_name_raises_attribute_error():
    from oa_configurator import refs

    with pytest.raises(AttributeError):
        _ = refs.not_a_name
