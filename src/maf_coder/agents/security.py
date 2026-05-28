"""SecurityWorkerAgent (AGENT_TOOLS_SPEC §10 + §17 step 12).

A BaseAgent subclass wiring the Security Worker tool surface. Like the
other Workers, structured output (the SecurityVerdict) is saved via
`save_security_verdict` during the run, not parsed from the LLM's
final_output. `parse_output` reports the closing narration plus the
verdict path (if one exists on disk).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schemas import Role
from .base import BaseAgent, TaskContext
from .tools.security_tools import build_security_tools


@dataclass(frozen=True)
class SecurityRunSummary:
    """Parsed output from one Security Worker run.

    `verdict_path` is the path of the saved security verdict (or empty
    when no verdict was saved). `notes_path` is the path of the
    human-readable notes companion.
    """

    final_message: str
    verdict_path: str = ""
    notes_path: str = ""
    tools_invoked: list[str] = field(default_factory=list)


class SecurityWorkerAgent(BaseAgent[SecurityRunSummary]):
    """Implements §10 — read-only, parallel-safe, severity-driven verdict."""

    role = Role.SECURITY_WORKER
    prompt_path = Path("prompts/security_worker.md")

    def build_tools(self, ctx: TaskContext) -> list[Any]:
        return build_security_tools(ctx)

    def build_first_user_message(self, ctx: TaskContext) -> str:
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
            "1. Run cheap tools first: cargo_audit, cargo_deny_check. Then geiger / secret scans.",
            "2. Each finding needs concrete evidence — no speculation.",
            "3. Save an empty verdict if every tool ran clean. That is a valid result.",
            "4. Note any missing scanners in security_notes/<task_id>.md.",
            "5. Final message: severity counts, blocks_pr y/n + why, missing scanners.",
        ]
        return "\n".join(lines)

    def parse_output(self, raw_output: str, ctx: TaskContext) -> SecurityRunSummary:
        verdict_rel = f"verdicts/{ctx.task.task_id}.security.json"
        notes_rel = f"security_notes/{ctx.task.task_id}.md"
        return SecurityRunSummary(
            final_message=raw_output.strip(),
            verdict_path=verdict_rel if ctx.store.exists(verdict_rel) else "",
            notes_path=notes_rel if ctx.store.exists(notes_rel) else "",
            tools_invoked=list(ctx.tools_invoked),
        )

    def _null_output(self) -> SecurityRunSummary:
        return SecurityRunSummary(
            final_message="",
            verdict_path="",
            notes_path="",
            tools_invoked=[],
        )


__all__ = ["SecurityRunSummary", "SecurityWorkerAgent"]
