"""OrchestratorAgent (AGENT_TOOLS_SPEC §6 + §17 step 6).

The Orchestrator is the planning and steering agent. It owns mission lifecycle,
DAG construction (via `dispatch_task`), milestone checkpoints, status reports,
escalations, and the final retro. It does NOT execute code inside the sandbox.

The scheduler is injected after construction (via `attach_scheduler`) because
MissionDriver constructs the Scheduler with references to the agents and we
want a clean two-phase wiring: Agent first, Scheduler second.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..memory import retrieve_memory_block
from ..schemas import Role
from .base import BaseAgent, TaskContext
from .tools.orchestrator_tools import _SchedulerLike, build_orchestrator_tools


@dataclass(frozen=True)
class OrchestratorRunSummary:
    """Parsed output from one Orchestrator run."""

    final_message: str
    tools_invoked: list[str] = field(default_factory=list)


class OrchestratorAgent(BaseAgent[OrchestratorRunSummary]):
    role = Role.ORCHESTRATOR
    prompt_path = Path("prompts/orchestrator.md")

    def __init__(self, *, scheduler: _SchedulerLike | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._scheduler: _SchedulerLike | None = scheduler

    def attach_scheduler(self, scheduler: _SchedulerLike) -> None:
        """Wire the scheduler late — MissionDriver calls this after construction."""
        self._scheduler = scheduler

    def build_tools(self, ctx: TaskContext) -> list[Any]:
        return build_orchestrator_tools(ctx, scheduler=self._scheduler)

    def build_first_user_message(self, ctx: TaskContext) -> str:
        task = ctx.task
        lines = [
            f"# Orchestrator task: {task.task_id}",
            "",
            "## Goal",
            task.goal,
            "",
            "## Background",
            task.background,
            "",
            "## Discipline",
            "1. Read existing mission state + artifacts before deciding.",
            "2. Use `dispatch_task` to schedule work. Validate acceptance_criteria "
            "exist in the locked validation_contract.yaml before dispatching.",
            "3. Use `create_checkpoint` at milestone boundaries.",
            "4. Use `escalate_to_human_gate` for ambiguous decisions.",
            "5. Final message: one paragraph summary + next-action recommendation.",
        ]
        # Phase F — F-memory: inject prior-mission lessons as NON-binding context.
        # Cold-start safe: no db / any error ⇒ nothing appended, never crashes.
        memory_block = retrieve_memory_block(ctx.store, ctx.task.goal)
        if memory_block:
            lines += ["", memory_block]
        return "\n".join(lines)

    def parse_output(self, raw_output: str, ctx: TaskContext) -> OrchestratorRunSummary:
        return OrchestratorRunSummary(
            final_message=raw_output.strip(),
            tools_invoked=list(ctx.tools_invoked),
        )

    def _null_output(self) -> OrchestratorRunSummary:
        return OrchestratorRunSummary(final_message="", tools_invoked=[])


__all__ = ["OrchestratorAgent", "OrchestratorRunSummary"]
