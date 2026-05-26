"""Result dataclasses shared across tools (AGENT_TOOLS_SPEC §4).

These are the canonical return types the LLM sees as tool output. Keeping
them as frozen dataclasses (rather than dicts) prevents agents from depending
on incidental field ordering and lets `mypy` catch field renames.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a sandbox command execution.

    Used by every sandbox tool that wraps a shell command. Non-zero
    `exit_code` is NOT an exception — it is returned to the agent who decides
    how to react. `truncated_*` flags inform the agent that output was clipped
    and the full payload was redirected to events.jsonl for forensic replay.
    """

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float
    truncated_stdout: bool = False
    truncated_stderr: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class FileContent:
    """Outcome of a file read."""

    path: str
    content: str
    size_bytes: int
    truncated: bool = False


@dataclass(frozen=True)
class GrepMatch:
    """One match from a grep tool call."""

    path: str
    line_number: int
    line: str
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SanitizedContent:
    """Outcome of an external HTTP fetch with sanitizer applied.

    `original_url` is preserved for citation. `sanitization_actions` records
    what the sanitizer modified.
    """

    original_url: str
    final_url: str
    content: str
    content_type: str
    sanitization_actions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TaskHandle:
    """Handle returned by `dispatch_task` to identify a running task.

    Opaque task id plus the monotonic time the dispatch happened. NOT a
    thread/process handle — just enough for the Orchestrator to refer back
    to a task it just scheduled.
    """

    task_id: str
    dispatched_at: float


TaskStatus = Literal["pending", "ready", "active", "complete", "failed", "blocked"]


__all__ = [
    "CommandResult",
    "FileContent",
    "GrepMatch",
    "SanitizedContent",
    "TaskHandle",
    "TaskStatus",
]
