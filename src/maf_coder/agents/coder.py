"""CoderWorkerAgent (AGENT_TOOLS_SPEC §7 + §17 step 5).

A BaseAgent subclass that wires the Coder Worker tool surface to a runnable
agent. The Coder's "structured output" — the Handoff — is saved via the
`save_handoff` tool during the run, not parsed from the LLM's final_output.
`parse_output` therefore returns the final_output text as a simple
`CoderRunSummary` containing the closing message + the handoff path on disk
(if one was saved).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schemas import Role
from .base import BaseAgent, TaskContext
from .tools.coder_tools import build_coder_tools


@dataclass(frozen=True)
class CoderRunSummary:
    """Parsed output from one Coder run.

    `final_message` is the LLM's closing narration. `handoff_path` is the
    blackboard path of the saved handoff JSON, if `save_handoff` ran
    successfully. Empty when the Coder didn't save a handoff (a soft failure
    surfaced via `AgentResult.errored=True` from BaseAgent).
    """

    final_message: str
    handoff_path: str = ""
    tools_invoked: list[str] = field(default_factory=list)


class CoderWorkerAgent(BaseAgent[CoderRunSummary]):
    """Implements §7 — full sandbox-side tool surface, gates on the v3.1
    handoff-completeness rule via the saved handoff (read by ReviewValidator,
    not by this agent)."""

    role = Role.CODER_WORKER
    prompt_path = Path("prompts/coder_worker.md")

    def build_tools(self, ctx: TaskContext) -> list[Any]:
        return build_coder_tools(ctx)

    def build_first_user_message(self, ctx: TaskContext) -> str:
        """Canonical Coder kickoff message (WORKED_EXAMPLE.md, simplified).

        Includes: task goal, background, acceptance criteria, required
        outputs, and a reminder of the v3.1 handoff completeness rule.
        """
        task = ctx.task
        lines = [
            f"# Task: {task.task_id}",
            "",
            "## Goal",
            task.goal,
            "",
            "## Background",
            task.background,
            "",
            "## Acceptance criteria",
        ]
        for ac in task.acceptance_criteria:
            lines.append(f"- {ac}")
        lines += [
            "",
            "## Required outputs",
        ]
        for ro in task.required_outputs:
            lines.append(f"- {ro}")
        lines += [
            "",
            "## Discipline",
            "1. Read the validation contract via blackboard before editing.",
            "2. Run cargo gates (`cargo check`, `cargo test`, `cargo clippy`, `cargo fmt --check`) "
            "before saving the handoff.",
            "3. Save handoff with HONEST `incomplete`/`issues_discovered`/`deviations_from_plan`. "
            "A handoff with all three empty triggers adversarial second-pass review (v3.1).",
            "4. Save patch via `save_patch`. Save test report via `save_test_report`.",
            "5. Final message: one paragraph summarizing what you did and where to find it.",
        ]
        return "\n".join(lines)

    def parse_output(self, raw_output: str, ctx: TaskContext) -> CoderRunSummary:
        handoff_path = ""
        # Best-effort: pick up the path of the saved handoff if it exists.
        handoff_rel = f"handoff/{ctx.task.task_id}.json"
        if ctx.store.exists(handoff_rel):
            handoff_path = handoff_rel
        return CoderRunSummary(
            final_message=raw_output.strip(),
            handoff_path=handoff_path,
            tools_invoked=list(ctx.tools_invoked),
        )

    def _null_output(self) -> CoderRunSummary:  # type: ignore[override]
        return CoderRunSummary(final_message="", handoff_path="", tools_invoked=[])


__all__ = ["CoderWorkerAgent", "CoderRunSummary"]
