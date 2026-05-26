"""Errors hierarchy tests (AGENT_TOOLS_SPEC §3)."""
from __future__ import annotations

import pytest

from maf_coder.agents import (
    ArtifactError,
    AssertionUnknownError,
    BudgetExceededError,
    ExternalContentError,
    PermissionDeniedError,
    SandboxError,
    TaskAlreadyDispatchedError,
    ToolError,
)


class TestHierarchy:
    @pytest.mark.parametrize(
        "cls",
        [
            PermissionDeniedError,
            SandboxError,
            ArtifactError,
            ExternalContentError,
            BudgetExceededError,
            TaskAlreadyDispatchedError,
            AssertionUnknownError,
        ],
    )
    def test_inherits_from_tool_error(self, cls: type) -> None:
        assert issubclass(cls, ToolError)


class TestPermissionDeniedError:
    def test_carries_what_and_why(self) -> None:
        e = PermissionDeniedError("src/foo.rs", "not in allowed_paths")
        assert e.what == "src/foo.rs"
        assert e.why == "not in allowed_paths"
        assert "src/foo.rs" in str(e)
        assert "not in allowed_paths" in str(e)

    def test_can_be_raised_and_caught_as_tool_error(self) -> None:
        with pytest.raises(ToolError) as ei:
            raise PermissionDeniedError("x", "y")
        assert isinstance(ei.value, PermissionDeniedError)
