"""save_retro tool + Orchestrator retrieval-injection tests (Phase F — F-memory).

WHY: the tool is the mission-end hook that persists experience, and the
Orchestrator's first user message is the retrieval-injection point. We verify
end to end that a saved retro (a) writes mission_retro.md, (b) lands in the
per-repo ProjectMemory, and (c) is surfaced — framed NON-binding — into the
next Orchestrator message. We also verify the orchestrator-only permission gate
and cold-start safety (no db yet ⇒ no crash, no lessons block).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from maf_coder.agents.base import TaskContext
from maf_coder.agents.errors import PermissionDeniedError
from maf_coder.agents.orchestrator import _retrieve_memory_block
from maf_coder.agents.tools.orchestrator_tools import make_save_retro
from maf_coder.blackboard import ArtifactStore
from maf_coder.memory.paths import open_project_memory
from maf_coder.models.router import ModelRouter
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
        "  orchestrator:\n"
        "    primary: {model: openai/x, temperature: 0.1, max_tokens: 1000}\n"
        "    fallback: []\n"
        "  coder_worker:\n"
        "    primary: {model: anthropic/x, temperature: 0.1, max_tokens: 1000}\n"
        "    fallback: []\n"
        "  review_validator:\n"
        "    primary: {model: openai/x, temperature: 0.0, max_tokens: 1000}\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m-retro")


def _ctx(store: ArtifactStore, router: ModelRouter, owner: Role, goal: str = "add health") -> TaskContext:
    task = Task(
        task_id="orch-t1",
        parent_milestone="m1",
        owner=owner,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal=goal,
        background="bg",
        acceptance_criteria=[],
        required_outputs=["mission_retro.md"],
        permission=Permission(allowed_paths=["**"], network_policy=NetworkPolicy.NONE),
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=60),
    )
    return TaskContext(
        task=task,
        mission_id="m-retro",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=_StubSandbox(),
    )


class _StubSandbox:
    async def exec(self, cmd: str, *, cwd: str = "/workspace", timeout_sec: int = 60) -> Any:
        from maf_coder.agents.results import CommandResult

        return CommandResult(command=cmd, exit_code=0, stdout="", stderr="", duration_sec=0.0)


@pytest.mark.asyncio
async def test_save_retro_orchestrator_only(store: ArtifactStore, router: ModelRouter) -> None:
    coder_ctx = _ctx(store, router, Role.CODER_WORKER)
    with pytest.raises(PermissionDeniedError):
        await make_save_retro(coder_ctx)(goal="g")


@pytest.mark.asyncio
async def test_save_retro_writes_artifact_and_ingests(
    store: ArtifactStore, router: ModelRouter
) -> None:
    ctx = _ctx(store, router, Role.ORCHESTRATOR)
    out = await make_save_retro(ctx)(
        goal="add health endpoint",
        what_worked=["clean DI"],
        global_lessons=["lock the contract before coding"],
        modules=["api"],
    )
    assert out["records_ingested"] == 2  # 1 worked + 1 global lesson
    # mission_retro.md was written
    assert store.exists("mission_retro.md")
    assert "## What Worked" in store.read_text("mission_retro.md")

    # rows landed in the per-repo memory db
    memory = open_project_memory(store)
    try:
        assert memory.count() == 2
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_saved_retro_is_injected_into_next_orchestrator_message(
    store: ArtifactStore, router: ModelRouter
) -> None:
    save_ctx = _ctx(store, router, Role.ORCHESTRATOR)
    await make_save_retro(save_ctx)(
        goal="add health endpoint",
        global_lessons=["always lock the validation contract before coding"],
    )
    # A later mission task whose goal overlaps the lesson should surface it.
    next_ctx = _ctx(store, router, Role.ORCHESTRATOR, goal="lock validation contract for new feature")
    block = _retrieve_memory_block(next_ctx)
    assert "<historical_lesson" in block
    assert "NON-BINDING" in block
    assert "lock the validation contract" in block


def test_retrieval_injection_cold_start_safe(store: ArtifactStore, router: ModelRouter) -> None:
    # No memory db has ever been written for this repo -> empty block, no crash.
    ctx = _ctx(store, router, Role.ORCHESTRATOR, goal="brand new mission")
    assert _retrieve_memory_block(ctx) == ""
