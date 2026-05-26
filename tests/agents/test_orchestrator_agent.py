"""OrchestratorAgent test (AGENT_TOOLS_SPEC §17 step 6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from maf_coder.agents.base import _RawResult
from maf_coder.agents.orchestrator import OrchestratorAgent
from maf_coder.blackboard import ArtifactStore
from maf_coder.models.router import ModelRouter
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import (
    Assertion,
    Feature,
    NetworkPolicy,
    Permission,
    RiskLevel,
    Role,
    Task,
    TaskBudget,
    ValidationContract,
    VerificationMethod,
)


@pytest.fixture
def router(tmp_path: Path) -> ModelRouter:
    cfg = tmp_path / "droid.yaml"
    cfg.write_text(
        "version: 1\n"
        "roles:\n"
        "  orchestrator:\n"
        "    primary: {model: openai/x, temperature: 0.1, max_tokens: 1000}\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    s = ArtifactStore(tmp_path / "missions", "m-orch-agent")
    s.save_validation_contract(
        ValidationContract(
            mission_id="m-orch-agent",
            features=[
                Feature(
                    feature_id="f1",
                    description="health",
                    assertions=[
                        Assertion(
                            id="f1.a1",
                            statement="GET /health returns 200",
                            verification_method=VerificationMethod.INTEGRATION_TEST,
                            verification_target="tests/h.rs::ok",
                        )
                    ],
                )
            ],
        )
    )
    return s


class _StubOrchAgent(OrchestratorAgent):
    prompt_path = Path("prompts/orchestrator.md")

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
        save = next(t for t in tools if t.__name__ == "save_artifact")
        await save(path="plan.md", content="# plan\n- impl feature 1\n")
        return _RawResult(
            final_output="Planning complete.",
            tokens_in=5,
            tokens_out=10,
            cost_usd=0.001,
            model_used="openai/x",
        )


@pytest.mark.asyncio
async def test_orchestrator_saves_plan(tmp_path, router, store) -> None:
    sandbox = LocalShellSandbox()
    await sandbox.start(workspace_mount=tmp_path / "ws")
    try:
        agent = _StubOrchAgent(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
        )
        task = Task(
            task_id="orch-t1",
            parent_milestone="m1",
            owner=Role.ORCHESTRATOR,
            priority=RiskLevel.MEDIUM,
            risk_level=RiskLevel.LOW,
            goal="plan mission",
            background="bg",
            acceptance_criteria=[],
            required_outputs=["plan.md"],
            permission=Permission(allowed_paths=["**"], network_policy=NetworkPolicy.NONE),
            budget=TaskBudget(max_tokens=1000, max_runtime_sec=30),
        )
        result = await agent.run(task, mission_id="m-orch-agent")
        assert not result.errored
        assert store.exists("plan.md")
        assert "save_artifact" in result.tools_invoked
    finally:
        await sandbox.stop()
