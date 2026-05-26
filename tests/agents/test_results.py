"""Result dataclasses tests (AGENT_TOOLS_SPEC §4)."""

from __future__ import annotations

import pytest

from maf_coder.agents import CommandResult, FileContent, GrepMatch, TaskHandle


class TestCommandResult:
    def test_basic(self) -> None:
        r = CommandResult(
            command="cargo test",
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_sec=1.5,
        )
        assert r.ok is True
        assert r.truncated_stdout is False
        assert r.truncated_stderr is False

    def test_non_zero_is_not_ok(self) -> None:
        r = CommandResult(
            command="cargo test", exit_code=1, stdout="", stderr="fail", duration_sec=0.1
        )
        assert r.ok is False

    def test_frozen(self) -> None:
        r = CommandResult(command="x", exit_code=0, stdout="", stderr="", duration_sec=0.0)
        with pytest.raises(Exception):
            r.exit_code = 5  # type: ignore[misc]


class TestFileContent:
    def test_truncation_flag(self) -> None:
        fc = FileContent(path="src/foo.rs", content="x" * 100, size_bytes=1_500_000, truncated=True)
        assert fc.truncated is True


class TestGrepMatch:
    def test_defaults(self) -> None:
        m = GrepMatch(path="src/foo.rs", line_number=10, line="fn main()")
        assert m.context_before == []
        assert m.context_after == []


class TestTaskHandle:
    def test_fields(self) -> None:
        h = TaskHandle(task_id="t1", dispatched_at=12345.6)
        assert h.task_id == "t1"
        assert h.dispatched_at == 12345.6
