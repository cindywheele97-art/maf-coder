"""BehaviorValidatorAgent (Phase D PR-D2; mirrors review.py).

A BaseAgent subclass wiring the BehaviorValidator tool surface (§11). Like the
ReviewValidator, the structured output (the BehaviorVerdict) is saved via
`save_behavior_verdict` / `run_behavior_probes` during the run, not parsed from
the LLM's final_output. `parse_output` reports the closing narration plus the
verdict path (if one exists on disk).

The BehaviorValidator is read-only on source: it starts services, runs probes,
captures evidence, and writes exactly a verdict + its evidence. It runs only
after the corresponding ReviewValidator verdict is PASS (the runtime gate is
PR-D3's job; this agent only states the expectation in its first message).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schemas import Role
from .base import BaseAgent, TaskContext
from .tools.behavior_tools import build_behavior_tools


@dataclass(frozen=True)
class BehaviorRunSummary:
    """Parsed output from one BehaviorValidator run.

    `verdict_path` is the on-disk verdict JSON if `save_behavior_verdict` (or
    `run_behavior_probes`) was invoked. Empty when the validator failed to save
    one (surfaced via AgentResult.errored).
    """

    final_message: str
    verdict_path: str = ""
    tools_invoked: list[str] = field(default_factory=list)


class BehaviorValidatorAgent(BaseAgent[BehaviorRunSummary]):
    role = Role.BEHAVIOR_VALIDATOR
    prompt_path = Path("prompts/behavior_validator.md")

    def build_tools(self, ctx: TaskContext) -> list[Any]:
        return build_behavior_tools(ctx)

    def build_first_user_message(self, ctx: TaskContext) -> str:
        task = ctx.task
        # Heuristic: extract the Coder task being validated from input_artifacts
        # (the patch / review verdict whose behavior we probe).
        coder_target = ""
        for ia in task.input_artifacts:
            if ia.startswith("patches/"):
                coder_target = ia.split("/", 1)[1].split(".", 1)[0]
                break
            if ia.startswith("verdicts/") and ia.endswith(".review.json"):
                coder_target = ia.split("/", 1)[1].split(".", 1)[0]
                break
        lines = [
            f"# BehaviorValidator task: {task.task_id}",
            "",
            "## Goal",
            task.goal,
            "",
            "## Background",
            task.background,
            "",
            "## Discipline (soul.md §3.6 + v3.1)",
            "1. You are READ-ONLY on source. You NEVER edit code, tests, or "
            "config — you run the artifact and record what it does.",
            "2. Confirm the ReviewValidator verdict (verdicts/<t_review>.review.json) "
            "is PASS before probing. A non-PASS review means the implementation "
            "path is still in question — do not behavior-probe it. (The runtime "
            "gate is enforced upstream; you state the expectation.)",
            "3. Load project_profile.yaml and read behavior_probe.strategy; confirm "
            "it matches the project type (cli/backend/library/embedded/wasm).",
            "4. Load validation_contract.yaml (read-only — never mutate it) and "
            "identify the assertions whose verification_method == behavior_probe.",
            "5. Run `run_behavior_probes(task_id)`: it dispatches the strategy, "
            "emits one BehaviorObservation per assertion (1:1, order-preserving), "
            "writes evidence on the fail path, and saves the BehaviorVerdict.",
            "6. On FAIL, evidence is mandatory — the verdict's evidence_path must "
            "point at behavior_evidence/<task_id>. Stop any service you started.",
        ]
        if coder_target:
            lines += [
                "",
                f"## Coder task under behavior validation: {coder_target}",
                f"Review verdict (must be PASS): verdicts/{coder_target}.review.json",
                "Contract: validation_contract.yaml",
                "Profile: project_profile.yaml",
            ]
        return "\n".join(lines)

    def parse_output(self, raw_output: str, ctx: TaskContext) -> BehaviorRunSummary:
        verdict_rel = f"verdicts/{ctx.task.task_id}.behavior.json"
        verdict_path = verdict_rel if ctx.store.exists(verdict_rel) else ""
        return BehaviorRunSummary(
            final_message=raw_output.strip(),
            verdict_path=verdict_path,
            tools_invoked=list(ctx.tools_invoked),
        )

    def _null_output(self) -> BehaviorRunSummary:
        return BehaviorRunSummary(final_message="", verdict_path="", tools_invoked=[])


__all__ = ["BehaviorRunSummary", "BehaviorValidatorAgent"]
