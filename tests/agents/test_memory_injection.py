"""Phase F follow-up: prior-mission memory is injected into the Research and
Coder workers' first user messages (not just the Orchestrator).

Each test seeds the per-repo memory db with a relevant record, then asserts the
agent's `build_first_user_message` surfaces it as a NON-binding block — and that
a cold start (no db) injects nothing and never crashes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# build_first_user_message needs a bound TaskContext; import lazily-friendly.
from maf_coder.agents.base import TaskContext
from maf_coder.agents.coder import CoderWorkerAgent
from maf_coder.agents.research import ResearchWorkerAgent
from maf_coder.blackboard import ArtifactStore
from maf_coder.memory.paths import open_project_memory
from maf_coder.models import ModelRouter
from maf_coder.schemas import (
    MemoryRecord,
    NetworkPolicy,
    Permission,
    Role,
    Task,
    TaskBudget,
)
from maf_coder.schemas.common import RiskLevel


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
        "  research_worker:\n"
        "    primary: {model: google/x, temperature: 0.1, max_tokens: 1000}\n"
        "    fallback: []\n"
        "  review_validator:\n"
        "    primary: {model: google/x, temperature: 0.0, max_tokens: 1000}\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m-inject")


class _StubSandbox:
    async def exec(self, cmd: str, *, cwd: str = "/workspace", timeout_sec: int = 60) -> Any:
        from maf_coder.agents.results import CommandResult

        return CommandResult(command=cmd, exit_code=0, stdout="", stderr="", duration_sec=0.0)


def _task(owner: Role, goal: str) -> Task:
    return Task(
        task_id="t1",
        parent_milestone="m1",
        owner=owner,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal=goal,
        background="bg",
        acceptance_criteria=["it works"],
        required_outputs=["handoff.md"],
        permission=Permission(allowed_paths=["**"], network_policy=NetworkPolicy.NONE),
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=60),
    )


def _ctx(store: ArtifactStore, router: ModelRouter, owner: Role, goal: str) -> TaskContext:
    return TaskContext(
        task=_task(owner, goal),
        mission_id="m-inject",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=_StubSandbox(),
    )


def _seed(store: ArtifactStore, *, kind: str, text: str) -> None:
    memory = open_project_memory(store)
    try:
        memory.insert(
            MemoryRecord(
                record_id=f"r-{kind}",
                mission_id="m-prior",
                kind=kind,
                text=text,
                tags=text.lower().split(),
            )
        )
    finally:
        memory.close()


def test_coder_injects_prior_handoff(store: ArtifactStore, router: ModelRouter) -> None:
    """A prior handoff matching the coder's goal surfaces as a non-binding block."""
    _seed(store, kind="handoff", text="adding health endpoint to the axum router needed a new route")
    agent = CoderWorkerAgent(
        store=store, event_log=store.event_log(), router=router, sandbox=_StubSandbox()
    )
    msg = agent.build_first_user_message(
        _ctx(store, router, Role.CODER_WORKER, "add health endpoint to axum router")
    )
    assert "<historical_lesson" in msg
    assert "NON-BINDING" in msg
    assert "axum router" in msg


def test_research_injects_prior_lesson(store: ArtifactStore, router: ModelRouter) -> None:
    """A prior record matching the research goal surfaces for the Research worker."""
    _seed(store, kind="retro", text="tokio runtime flavor matters for blocking calls in handlers")
    agent = ResearchWorkerAgent(
        store=store, event_log=store.event_log(), router=router, sandbox=_StubSandbox()
    )
    msg = agent.build_first_user_message(
        _ctx(store, router, Role.RESEARCH_WORKER, "research tokio runtime flavor tradeoffs")
    )
    assert "<historical_lesson" in msg
    assert "tokio runtime" in msg


def test_coder_cold_start_injects_nothing(store: ArtifactStore, router: ModelRouter) -> None:
    """No memory db for this repo → no block appended, message still builds."""
    agent = CoderWorkerAgent(
        store=store, event_log=store.event_log(), router=router, sandbox=_StubSandbox()
    )
    msg = agent.build_first_user_message(
        _ctx(store, router, Role.CODER_WORKER, "brand new unrelated task")
    )
    assert "<historical_lesson" not in msg
    assert "# Task: t1" in msg  # the real message still rendered


def test_research_cold_start_injects_nothing(store: ArtifactStore, router: ModelRouter) -> None:
    agent = ResearchWorkerAgent(
        store=store, event_log=store.event_log(), router=router, sandbox=_StubSandbox()
    )
    msg = agent.build_first_user_message(
        _ctx(store, router, Role.RESEARCH_WORKER, "brand new unrelated task")
    )
    assert "<historical_lesson" not in msg
