"""Tests for package_base.py — PackageConfigBase factory interface."""

from __future__ import annotations

from typing import ClassVar

import pytest

from oa_configurator import PackageConfigBase, StackConfig


class SampleConfig(PackageConfigBase):
    tool_name: ClassVar[str] = "sample_tool"
    backend: str = "default_backend"
    file_path: str | None = None


class TestPackageConfigBase:
    def test_from_stack_reads_extra(self):
        cfg = StackConfig.for_session(
            tools={"sample_tool": {"extra": {"backend": "custom", "file_path": "/data"}}}
        )
        sample = SampleConfig.from_stack(cfg)
        assert sample.backend == "custom"
        assert sample.file_path == "/data"

    def test_from_stack_uses_defaults_when_tool_missing(self):
        cfg = StackConfig.for_session()
        sample = SampleConfig.from_stack(cfg)
        assert sample.backend == "default_backend"
        assert sample.file_path is None

    def test_from_stack_uses_defaults_when_extra_empty(self):
        cfg = StackConfig.for_session(
            tools={"sample_tool": {"extra": {}}}
        )
        sample = SampleConfig.from_stack(cfg)
        assert sample.backend == "default_backend"

    def test_to_extra_dict_excludes_none(self):
        sample = SampleConfig(backend="custom", file_path=None)
        d = sample.to_extra_dict()
        assert d == {"backend": "custom"}
        assert "file_path" not in d

    def test_to_extra_dict_includes_set_values(self):
        sample = SampleConfig(backend="custom", file_path="/data")
        d = sample.to_extra_dict()
        assert d == {"backend": "custom", "file_path": "/data"}

    def test_round_trip(self):
        original = SampleConfig(backend="sqlitevec", file_path="/embeddings")
        extra = original.to_extra_dict()
        cfg = StackConfig.for_session(
            tools={"sample_tool": {"extra": extra}}
        )
        restored = SampleConfig.from_stack(cfg)
        assert restored.backend == "sqlitevec"
        assert restored.file_path == "/embeddings"

    def test_subclass_must_set_tool_name(self):
        with pytest.raises((AttributeError, TypeError)):
            class BadConfig(PackageConfigBase):
                pass
            # Accessing tool_name on an instance should fail
            BadConfig().tool_name  # type: ignore[attr-defined]
