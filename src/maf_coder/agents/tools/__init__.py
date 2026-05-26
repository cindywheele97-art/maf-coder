"""Tool factories grouped by role.

Each tool family is in its own submodule:

- coder_tools.py        : Worker tools — file IO, run_bash, cargo_*, git_*,
                          save_patch / save_handoff / save_test_report
- review_tools.py       : ReviewValidator tools — apply_patch_in_fresh_worktree,
                          spawn_adversarial_subagent, save_review_verdict, …
- orchestrator_tools.py : Orchestrator tools — dispatch_task, save_artifact,
                          read_artifact, escalate_to_human_gate, …

Tools follow the factory pattern from AGENT_TOOLS_SPEC §3: each
`make_<tool_name>(ctx, …)` returns the actual tool callable, closed over
`ctx`. The shared helper `record_tool_call` appends to `ctx.tools_invoked`
and emits an EventLog `tool_call` event after the permission gate passes.
"""

from __future__ import annotations

import logging
import time

from ..base import TaskContext

logger = logging.getLogger(__name__)


def record_tool_call(
    ctx: TaskContext,
    tool: str,
    args_summary: str,
    *,
    exit_code: int | None = None,
    duration_sec: float | None = None,
) -> None:
    """Append the tool name to ctx.tools_invoked and log a tool_call event.

    Failure to log is logged but never propagated — observability MUST NOT
    affect tool correctness.
    """
    ctx.tools_invoked.append(tool)
    try:
        owner = ctx.task.owner
        actor = owner.value if hasattr(owner, "value") else str(owner)
        ctx.event_log.log_tool_call(
            mission_id=ctx.mission_id,
            actor=actor,
            tool=tool,
            args_summary=args_summary,
            exit_code=exit_code,
            duration_sec=duration_sec,
            task_id=ctx.task.task_id,
        )
    except Exception:
        logger.exception("event_log.log_tool_call failed for tool=%s; continuing", tool)


def time_block() -> float:
    """Return a monotonic clock — caller subtracts to get duration_sec."""
    return time.monotonic()


__all__ = ["record_tool_call", "time_block"]
