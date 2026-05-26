"""CoderWorkerAgent integration test.

Verifies the agent shell stitches together prompt + tools + run, by stubbing
out `_execute_sdk` to invoke a couple of tools as a real LLM-driven run
would.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maf_coder.agents.base import _RawResult
from maf_coder.agents.coder import CoderRunSummary, CoderWorkerAgent
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
        "  coder_worker:\n"
        "    primary:\n"
        "      model: anthropic/x\n"
        "      temperature: 0.1\n"
        "      max_tokens: 1000\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m-coder")


@pytest.fixture
async def sandbox(tmp_path: Path):
    sb = LocalShellSandbox()
    await sb.start(workspace_mount=tmp_path / "ws")
    await sb.exec(
        "git init -q -b main && git config user.email t@t && git config user.name t",
        cwd="/workspace",
    )
    await sb.exec("touch initial && git add -A && git commit -q -m initial", cwd="/workspace")
    try:
        yield sb
    finally:
        await sb.stop()


@pytest.fixture
def prompt(tmp_path: Path) -> Path:
    p = tmp_path / "coder_prompt.md"
    p.write_text("You are the Coder Worker. Edit code, run cargo, save handoff.")
    return p


def _task() -> Task:
    return Task(
        task_id="t1",
        parent_milestone="m1",
        owner=Role.CODER_WORKER,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="implement /health endpoint",
        background="server has no health endpoint yet",
        acceptance_criteria=["f1.a1: GET /health -> 200"],
        required_outputs=["patches/t1.diff", "handoff/t1.json"],
        permission=Permission(
            allowed_paths=["**"],
            allowed_tools=[],
            network_policy=NetworkPolicy.NONE,
        ),
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=30),
    )


class _ScriptedCoder(CoderWorkerAgent):
    """Drive the Coder by calling tools directly inside _execute_sdk.

    Simulates "LLM decides to write a file, run cargo test (we accept failure),
    save patch, save handoff" without actually invoking an LLM.
    """

    def __init__(self, *, prompt_path: Path, **kw):  # type: ignore[no-untyped-def]
        self.prompt_path = prompt_path
        super().__init__(**kw)

    async def _execute_sdk(self, **kw):  # type: ignore[override]
        ctx = kw["ctx"]
        tools_by_name = {t.__name__: t for t in self.build_tools(ctx)}
        await tools_by_name["write_file"](path="src/health.rs", content="// stub\n")
        await tools_by_name["save_patch"](task_id=ctx.task.task_id)
        await tools_by_name["save_handoff"](
            task_id=ctx.task.task_id,
            completed=["wrote stub for /health"],
            issues_discovered=["needs router glue"],
            next_recommended_action="send_to_review_validator",
        )
        return _RawResult(
            final_output="Wrote stub for /health; handoff saved.",
            tokens_in=50,
            tokens_out=30,
            cost_usd=0.0,
            model_used=kw["model_id"],
        )


class TestCoderWorkerAgent:
    def test_role_and_prompt(self, prompt: Path, store, router, sandbox) -> None:
        agent = _ScriptedCoder(
            prompt_path=prompt,
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
        )
        assert agent.role == Role.CODER_WORKER

    @pytest.mark.asyncio
    async def test_end_to_end_run(self, prompt: Path, store, router, sandbox) -> None:
        agent = _ScriptedCoder(
            prompt_path=prompt,
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
        )
        result = await agent.run(_task(), mission_id="m-coder")
        assert result.errored is False
        assert isinstance(result.parsed_output, CoderRunSummary)
        assert result.parsed_output.handoff_path == "handoff/t1.json"
        assert "write_file" in result.tools_invoked
        assert "save_patch" in result.tools_invoked
        assert "save_handoff" in result.tools_invoked

        # Handoff actually round-trips
        loaded = store.load_handoff("t1")
        assert loaded.task_id == "t1"
        assert loaded.completed == ["wrote stub for /health"]
        assert loaded.issues_discovered == ["needs router glue"]
