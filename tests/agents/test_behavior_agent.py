"""BehaviorValidatorAgent integration test (Phase D PR-D2; mirrors review)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from maf_coder.agents.base import _RawResult
from maf_coder.agents.behavior import BehaviorRunSummary, BehaviorValidatorAgent
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
        "  behavior_validator:\n"
        "    primary: {model: openai/x, temperature: 0.0, max_tokens: 1000}\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m-bv")


class _StubBehaviorAgent(BehaviorValidatorAgent):
    """BehaviorValidator that drives a scripted "saved verdict" flow without SDK."""

    prompt_path = Path("prompts/behavior_validator.md")

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
        save_verdict = next(t for t in tools if t.__name__ == "save_behavior_verdict")
        await save_verdict(
            task_id=ctx.task.task_id,
            result="pass",
            probe_strategy="cli_assert_cmd_probe",
            observations=[
                {
                    "assertion_id": "f1.a1",
                    "observed": "0",
                    "expected": "0",
                    "matched": True,
                }
            ],
            evidence_path="",
            failure_reason=None,
        )
        return _RawResult(
            final_output="Behavior validation complete — verdict saved.",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.01,
            model_used="openai/x",
        )


def _behavior_task() -> Task:
    return Task(
        task_id="bv-t1",
        parent_milestone="m1",
        owner=Role.BEHAVIOR_VALIDATOR,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="validate behavior",
        background="bg",
        acceptance_criteria=["f1.a1"],
        required_outputs=["verdicts/bv-t1.behavior.json"],
        input_artifacts=["verdicts/bv-t1.review.json"],
        permission=Permission(allowed_paths=["**"], network_policy=NetworkPolicy.NONE),
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=30),
    )


@pytest.mark.asyncio
async def test_behavior_agent_end_to_end(tmp_path, router, store) -> None:
    sandbox = LocalShellSandbox()
    await sandbox.start(workspace_mount=tmp_path / "ws")
    try:
        agent = _StubBehaviorAgent(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
        )
        result = await agent.run(
            _behavior_task(), mission_id="m-bv", coder_provider_in_use="anthropic"
        )

        assert not result.errored, result.error_reason
        assert isinstance(result.parsed_output, BehaviorRunSummary)
        assert result.parsed_output.verdict_path == "verdicts/bv-t1.behavior.json"
        assert "save_behavior_verdict" in result.tools_invoked
        verdict = store.load_behavior_verdict("bv-t1")
        assert verdict.result == "pass"
        assert verdict.probe_strategy == "cli_assert_cmd_probe"
        assert [o.assertion_id for o in verdict.observations] == ["f1.a1"]
    finally:
        await sandbox.stop()


def test_behavior_agent_role_and_tools_wiring(router, store) -> None:
    """Role is BEHAVIOR_VALIDATOR and the tool set is the read-only probe surface.

    The behavior validator must wire build_behavior_tools (probes + verdict
    writers) and carry the behavior_validator role so the router applies the
    different-provider constraint (invariant §1.5).
    """
    sandbox = LocalShellSandbox()
    agent = BehaviorValidatorAgent(
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
    )
    assert agent.role == Role.BEHAVIOR_VALIDATOR

    from maf_coder.agents.base import TaskContext

    ctx = TaskContext(
        task=_behavior_task(),
        mission_id="m-bv",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
    )
    tool_names = {t.__name__ for t in agent.build_tools(ctx)}
    assert "save_behavior_verdict" in tool_names
    assert "run_behavior_probes" in tool_names
    # Read-only on source: no source-mutating tools are exposed.
    assert not {"edit_file", "write_file", "apply_patch", "git_commit"} & tool_names


def test_behavior_validator_honors_different_provider_rule() -> None:
    """Invariant §1.5 — behavior_validator must run ≠ the Coder's provider.

    The dynamic half of the异-provider rule lives in the router's validator-role
    set; behavior_validator must be in it, exactly as review_validator is.
    """
    from maf_coder.models.router import _VALIDATOR_ROLES

    assert Role.BEHAVIOR_VALIDATOR.value in _VALIDATOR_ROLES


def test_behavior_first_message_is_read_only_and_states_review_gate(router, store) -> None:
    """The first user message must assert read-only-on-source and the review-PASS gate."""
    from maf_coder.agents.base import TaskContext

    sandbox = LocalShellSandbox()
    agent = BehaviorValidatorAgent(
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
    )
    ctx = TaskContext(
        task=_behavior_task(),
        mission_id="m-bv",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
    )
    msg = agent.build_first_user_message(ctx)
    assert "READ-ONLY" in msg
    assert "NEVER edit code" in msg
    assert "verdicts/bv-t1.review.json" in msg
    assert "PASS" in msg
