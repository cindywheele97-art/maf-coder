"""ReviewValidatorAgent (AGENT_TOOLS_SPEC §8 + §17 step 6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schemas import Role
from .base import BaseAgent, TaskContext
from .tools.review_tools import build_review_tools


@dataclass(frozen=True)
class ReviewRunSummary:
    """Parsed output from one ReviewValidator run.

    `verdict_path` is the on-disk verdict JSON if `save_review_verdict` was
    invoked. Empty when the validator failed to save one (surfaced via
    AgentResult.errored).
    """

    final_message: str
    verdict_path: str = ""
    tools_invoked: list[str] = field(default_factory=list)


class ReviewValidatorAgent(BaseAgent[ReviewRunSummary]):
    role = Role.REVIEW_VALIDATOR
    prompt_path = Path("prompts/review_validator.md")

    def build_tools(self, ctx: TaskContext) -> list[Any]:
        return build_review_tools(ctx)

    def build_first_user_message(self, ctx: TaskContext) -> str:
        task = ctx.task
        coder_task_id = ctx.task.task_id  # validator runs in its own task
        # Heuristic: extract the Coder task being reviewed from input_artifacts.
        coder_target = ""
        for ia in task.input_artifacts:
            if ia.startswith("patches/"):
                coder_target = ia.split("/", 1)[1].split(".", 1)[0]
                break
        lines = [
            f"# ReviewValidator task: {task.task_id}",
            "",
            "## Goal",
            task.goal,
            "",
            "## Background",
            task.background,
            "",
            "## Discipline (soul.md §3.5 + v3.1)",
            "1. Read the patch under review (patches/<coder_task>.diff) and the Coder's handoff.",
            "2. Apply the patch in a fresh worktree (`apply_patch_in_fresh_worktree`).",
            "3. Re-run the cargo gate set: cargo_build, cargo_test (+ doc), cargo_clippy, cargo_fmt --check.",
            "4. If the handoff's `triggers_second_pass` is True (no incomplete, "
            "no issues, no deviations), run `spawn_adversarial_subagent(purpose="
            "'completeness_second_pass')` — it MUST run on a different provider.",
            "5. Run `spawn_adversarial_subagent(purpose='intent_test_detection')` on "
            "the new tests to detect hardcoded-result tests.",
            "6. Save the verdict via `save_review_verdict` and any narrative reasoning "
            "via `save_review_notes`.",
        ]
        if coder_target:
            lines += [
                "",
                f"## Coder task under review: {coder_target}",
                f"Patch: patches/{coder_target}.diff",
                f"Handoff: handoff/{coder_target}.json",
            ]
        del coder_task_id  # not used in canonical form, but documented for future
        return "\n".join(lines)

    def parse_output(self, raw_output: str, ctx: TaskContext) -> ReviewRunSummary:
        verdict_rel = f"verdicts/{ctx.task.task_id}.review.json"
        verdict_path = verdict_rel if ctx.store.exists(verdict_rel) else ""
        return ReviewRunSummary(
            final_message=raw_output.strip(),
            verdict_path=verdict_path,
            tools_invoked=list(ctx.tools_invoked),
        )

    def _null_output(self) -> ReviewRunSummary:  # type: ignore[override]
        return ReviewRunSummary(final_message="", verdict_path="", tools_invoked=[])


__all__ = ["ReviewRunSummary", "ReviewValidatorAgent"]
