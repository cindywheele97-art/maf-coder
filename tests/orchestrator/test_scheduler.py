"""Scheduler tests (AGENT_TOOLS_SPEC §13)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from maf_coder.agents.base import AgentResult, BaseAgent, TaskContext
from maf_coder.blackboard import ArtifactStore
from maf_coder.models.router import ModelRouter
from maf_coder.orchestrator.scheduler import Scheduler
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import (
    MissionState,
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
        "    primary: {model: anthropic/x, temperature: 0.1, max_tokens: 1000}\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m-sched")


class _FakeAgent(BaseAgent[str]):
    role = Role.CODER_WORKER
    prompt_path = Path("prompts/coder_worker.md")

    def __init__(
        self, *, store, event_log, router, sandbox, outcome: str = "ok", fail_first_n: int = 0
    ) -> None:
        super().__init__(store=store, event_log=event_log, router=router, sandbox=sandbox)
        self.outcome = outcome
        self.fail_first_n = fail_first_n
        self.calls = 0

    def build_tools(self, ctx: TaskContext) -> list[Any]:
        return []

    def build_first_user_message(self, ctx: TaskContext) -> str:
        return "go"

    def parse_output(self, raw_output: str, ctx: TaskContext) -> str:
        return raw_output

    def _null_output(self) -> str:
        return ""

    async def run(
        self, task, *, mission_id: str, coder_provider_in_use: str | None = None
    ) -> AgentResult[str]:
        self.calls += 1
        errored = self.calls <= self.fail_first_n or self.outcome == "fail"
        return AgentResult(
            role=self.role,
            task_id=task.task_id,
            parsed_output="" if errored else self.outcome,
            raw_output="",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_sec=0.0,
            model_used="anthropic/x",
            fallback_used=False,
            tools_invoked=[],
            errored=errored,
            error_reason="simulated" if errored else None,
        )


def _task(tid: str, depends_on: list[str] | None = None, retry: int = 0) -> Task:
    return Task(
        task_id=tid,
        parent_milestone="m1",
        owner=Role.CODER_WORKER,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="x",
        background="x",
        acceptance_criteria=["f1.a1"],
        required_outputs=["x"],
        permission=Permission(allowed_paths=["**"], network_policy=NetworkPolicy.NONE),
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=30),
        depends_on=depends_on or [],
        failure_handling={"retry_budget": retry},  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_runs_single_task(tmp_path, router, store) -> None:
    sandbox = LocalShellSandbox()
    await sandbox.start(workspace_mount=tmp_path / "ws")
    try:
        agent = _FakeAgent(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
        )
        sched = Scheduler(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
            agent_factory={Role.CODER_WORKER: lambda: agent},
            mission_id="m-sched",
        )
        await sched.add_task(_task("t1"))
        await sched.run()
        assert sched.task_status("t1") == "complete"
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_dependency_order(tmp_path, router, store) -> None:
    sandbox = LocalShellSandbox()
    await sandbox.start(workspace_mount=tmp_path / "ws")
    try:
        order: list[str] = []

        class TracingAgent(_FakeAgent):
            async def run(self, task, **kw):  # type: ignore[override]
                order.append(task.task_id)
                return await super().run(task, **kw)

        agent = TracingAgent(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
        )
        sched = Scheduler(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
            agent_factory={Role.CODER_WORKER: lambda: agent},
            mission_id="m-sched",
        )
        await sched.add_task(_task("t1"))
        await sched.add_task(_task("t2", depends_on=["t1"]))
        await sched.run()
        assert order == ["t1", "t2"]
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_retries_then_succeeds(tmp_path, router, store) -> None:
    sandbox = LocalShellSandbox()
    await sandbox.start(workspace_mount=tmp_path / "ws")
    try:
        agent = _FakeAgent(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
            fail_first_n=1,
        )
        sched = Scheduler(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
            agent_factory={Role.CODER_WORKER: lambda: agent},
            mission_id="m-sched",
        )
        await sched.add_task(_task("t1", retry=2))
        await sched.run()
        assert sched.task_status("t1") == "complete"
        assert agent.calls == 2
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_dependent_blocked_when_upstream_fails(tmp_path, router, store) -> None:
    sandbox = LocalShellSandbox()
    await sandbox.start(workspace_mount=tmp_path / "ws")
    try:
        agent = _FakeAgent(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
            outcome="fail",
        )
        sched = Scheduler(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
            agent_factory={Role.CODER_WORKER: lambda: agent},
            mission_id="m-sched",
        )
        await sched.add_task(_task("t1"))
        await sched.add_task(_task("t2", depends_on=["t1"]))
        await sched.run()
        assert sched.task_status("t1") == "failed"
        assert sched.task_status("t2") == "blocked"
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_coder_slot_serializes(tmp_path, router, store) -> None:
    """Two independent CODER_WORKER tasks must NOT run concurrently."""
    sandbox = LocalShellSandbox()
    await sandbox.start(workspace_mount=tmp_path / "ws")
    try:
        in_flight = 0
        peak = 0
        ev = asyncio.Event()

        class WatchingAgent(_FakeAgent):
            async def run(self, task, **kw):  # type: ignore[override]
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                # Yield briefly so the scheduler tries to dispatch the next one.
                await asyncio.sleep(0.05)
                in_flight -= 1
                if peak >= 1 and not ev.is_set():
                    ev.set()
                return await super().run(task, **kw)

        agent = WatchingAgent(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
        )
        sched = Scheduler(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
            agent_factory={Role.CODER_WORKER: lambda: agent},
            mission_id="m-sched",
        )
        await sched.add_task(_task("t1"))
        await sched.add_task(_task("t2"))
        await sched.run()
        assert peak == 1, f"coder slot violated: peak={peak}"
    finally:
        await sandbox.stop()


# -- Phase E §E5 — budget pause gate ---------------------------------------


def _save_state(store: ArtifactStore, *, budget_mode: str) -> None:
    from datetime import UTC, datetime

    store.save_mission_state(
        MissionState(
            mission_id=store.mission_id,
            started_at=datetime.now(UTC),
            budget_mode=budget_mode,
        )
    )


@pytest.mark.asyncio
async def test_paused_refuses_new_dispatch(tmp_path, router, store) -> None:
    """budget_mode == 'paused' → NEW tasks are blocked, never run."""
    _save_state(store, budget_mode="paused")
    sandbox = LocalShellSandbox()
    await sandbox.start(workspace_mount=tmp_path / "ws")
    try:
        agent = _FakeAgent(
            store=store, event_log=store.event_log(), router=router, sandbox=sandbox
        )
        sched = Scheduler(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
            agent_factory={Role.CODER_WORKER: lambda: agent},
            mission_id=store.mission_id,
        )
        await sched.add_task(_task("t1"))
        await sched.run()
        assert sched.task_status("t1") == "blocked"
        assert agent.calls == 0  # the agent never ran
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_normal_mode_dispatches(tmp_path, router, store) -> None:
    """budget_mode == 'normal' → tasks run as usual (pause gate is inert)."""
    _save_state(store, budget_mode="normal")
    sandbox = LocalShellSandbox()
    await sandbox.start(workspace_mount=tmp_path / "ws")
    try:
        agent = _FakeAgent(
            store=store, event_log=store.event_log(), router=router, sandbox=sandbox
        )
        sched = Scheduler(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
            agent_factory={Role.CODER_WORKER: lambda: agent},
            mission_id=store.mission_id,
        )
        await sched.add_task(_task("t1"))
        await sched.run()
        assert sched.task_status("t1") == "complete"
        assert agent.calls == 1
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_no_mission_state_is_not_paused(tmp_path, router, store) -> None:
    """Missing mission_state.json → fail-open (NOT paused); task runs."""
    sandbox = LocalShellSandbox()
    await sandbox.start(workspace_mount=tmp_path / "ws")
    try:
        agent = _FakeAgent(
            store=store, event_log=store.event_log(), router=router, sandbox=sandbox
        )
        sched = Scheduler(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
            agent_factory={Role.CODER_WORKER: lambda: agent},
            mission_id=store.mission_id,
        )
        await sched.add_task(_task("t1"))
        await sched.run()
        assert sched.task_status("t1") == "complete"
    finally:
        await sandbox.stop()
