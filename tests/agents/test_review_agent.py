"""ReviewValidatorAgent integration test (AGENT_TOOLS_SPEC §8 + §17)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from maf_coder.agents.base import _RawResult
from maf_coder.agents.review import ReviewValidatorAgent
from maf_coder.blackboard import ArtifactStore
from maf_coder.models.router import ModelRouter
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import (
    NetworkPolicy,
    Permission,
    RiskLevel,
    Role,
    Task,
    TaskBudget,
)


@pytest.fixture
def router(tmp_path: Path) -> ModelRouter:
    cfg = tmp_path / "droid.yaml"
    cfg.write_text(
        "version: 1\n"
        "roles:\n"
        "  review_validator:\n"
        "    primary: {model: openai/x, temperature: 0.0, max_tokens: 1000}\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m-rv")


class _StubReviewAgent(ReviewValidatorAgent):
    """ReviewValidator that drives a scripted "saved verdict" flow without SDK."""

    prompt_path = Path("prompts/review_validator.md")

    async def _execute_sdk(  # type: ignore[override]
        self,
        *,
        instructions: str,
        tools: list[Any],
        first_user_message: str,
        model_id: str,
        temperature: float,
        max_tokens: int,
        ctx,
    ) -> _RawResult:
        save_verdict = next(t for t in tools if t.__name__ == "save_review_verdict")
        save_notes = next(t for t in tools if t.__name__ == "save_review_notes")
        await save_verdict(
            task_id=ctx.task.task_id,
            result="pass",
            precise_reason="all gates green",
            next_action_recommendation="send_to_behavior_validator",
            cargo_gate_results={"build": True, "test": True, "clippy": True, "fmt": True},
        )
        await save_notes(
            task_id=ctx.task.task_id,
            notes_markdown="# Review notes\nlooks good\n",
        )
        return _RawResult(
            final_output="Review complete — verdict saved.",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.01,
            model_used="openai/x",
        )


@pytest.mark.asyncio
async def test_review_agent_end_to_end(tmp_path, router, store) -> None:
    sandbox = LocalShellSandbox()
    await sandbox.start(workspace_mount=tmp_path / "ws")
    try:
        agent = _StubReviewAgent(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
        )
        task = Task(
            task_id="rv-t1",
            parent_milestone="m1",
            owner=Role.REVIEW_VALIDATOR,
            priority=RiskLevel.MEDIUM,
            risk_level=RiskLevel.LOW,
            goal="review",
            background="bg",
            acceptance_criteria=["f1.a1"],
            required_outputs=["verdicts/rv-t1.review.json"],
            permission=Permission(allowed_paths=["**"], network_policy=NetworkPolicy.NONE),
            budget=TaskBudget(max_tokens=1000, max_runtime_sec=30),
        )
        result = await agent.run(task, mission_id="m-rv", coder_provider_in_use="anthropic")

        assert not result.errored, result.error_reason
        assert result.parsed_output.verdict_path == "verdicts/rv-t1.review.json"
        assert "save_review_verdict" in result.tools_invoked
        verdict = store.load_review_verdict("rv-t1")
        assert verdict.result == "pass"
    finally:
        await sandbox.stop()
