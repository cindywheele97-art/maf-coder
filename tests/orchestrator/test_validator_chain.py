"""Dual-validator chain gate tests (Phase D PR-D3).

The chain rule (soul.md §3.6, execution plan §2 PR-D3): a behavior_validator
task may run only AFTER its review_validator dependency produces a PASS verdict.
The gate is enforced in two complementary layers, both exercised here:

1. dispatch_task — STRUCTURAL gate (condition (a)): a behavior task that does
   not depend on any review_validator task is refused at dispatch time. The
   verdict does not exist yet at DAG-construction time, so only the dependency
   shape is checkable here.
2. Scheduler.run — RUNTIME verdict gate (condition (b)): once the review
   dependency is complete, the behavior task launches only if
   verdicts/<review_id>.review.json exists and result == PASS. Otherwise it is
   marked `blocked` and a validator_chain_blocked event carrying the
   implementation_path_issue signal is emitted (the D4 arbitration hook).

Both the review-task-id resolution and the gate are GENERIC: they match on the
owner role (review_validator), never on literal task IDs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from maf_coder.agents.base import AgentResult, BaseAgent, TaskContext
from maf_coder.agents.errors import ValidatorChainError
from maf_coder.agents.results import TaskHandle
from maf_coder.agents.tools.orchestrator_tools import make_dispatch_task
from maf_coder.blackboard import ArtifactStore
from maf_coder.models.router import ModelRouter
from maf_coder.orchestrator.scheduler import Scheduler
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import (
    Assertion,
    CargoGateResults,
    Feature,
    MissionState,
    NetworkPolicy,
    Permission,
    ReviewVerdict,
    RiskLevel,
    Role,
    Task,
    TaskBudget,
    ValidationContract,
    VerdictResult,
    VerificationMethod,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def router(tmp_path: Path) -> ModelRouter:
    cfg = tmp_path / "droid.yaml"
    cfg.write_text(
        "version: 1\n"
        "roles:\n"
        "  orchestrator:\n"
        "    primary: {model: openai/x, temperature: 0.1, max_tokens: 1000}\n"
        "    fallback: []\n"
        "  review_validator:\n"
        "    primary: {model: openai/x, temperature: 0.0, max_tokens: 1000}\n"
        "    fallback: []\n"
        "  behavior_validator:\n"
        "    primary: {model: openai/x, temperature: 0.0, max_tokens: 1000}\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    s = ArtifactStore(tmp_path / "missions", "m-chain")
    contract = ValidationContract(
        mission_id="m-chain",
        features=[
            Feature(
                feature_id="f1",
                description="health",
                assertions=[
                    Assertion(
                        id="f1.a1",
                        statement="GET /health returns 200",
                        verification_method=VerificationMethod.BEHAVIOR_PROBE,
                        verification_target="GET /health",
                    )
                ],
            )
        ],
    )
    s.save_validation_contract(contract)
    s.save_mission_state(
        MissionState(mission_id="m-chain", started_at=datetime.now(UTC))
    )
    return s


def _review_verdict(task_id: str, result: VerdictResult) -> ReviewVerdict:
    return ReviewVerdict(
        task_id=task_id,
        result=result,
        precise_reason="ok" if result == VerdictResult.PASS else "src/x.rs:1 broken",
        next_action_recommendation="send to behavior validator",
        cargo_gate_results=CargoGateResults(
            build=True, test=True, clippy=True, fmt=True
        ),
    )


def _task(
    tid: str,
    owner: Role,
    *,
    depends_on: list[str] | None = None,
) -> Task:
    return Task(
        task_id=tid,
        parent_milestone="m1",
        owner=owner,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="x",
        background="x",
        acceptance_criteria=["f1.a1"],
        required_outputs=["x"],
        permission=Permission(allowed_paths=["**"], network_policy=NetworkPolicy.NONE),
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=30),
        depends_on=depends_on or [],
    )


# ---------------------------------------------------------------------------
# Layer 1 — dispatch_task structural gate (condition (a))
# ---------------------------------------------------------------------------


def _orch_ctx(store: ArtifactStore, router: ModelRouter) -> TaskContext:
    task = _task("orch-t1", Role.ORCHESTRATOR)
    return TaskContext(
        task=task,
        mission_id="m-chain",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=_StubSandbox(),
    )


class _StubSandbox:
    async def exec(self, cmd: str, *, cwd: str = "/workspace", timeout_sec: int = 60):
        from maf_coder.agents.results import CommandResult

        return CommandResult(command=cmd, exit_code=0, stdout="", stderr="", duration_sec=0.0)


class _StubScheduler:
    """Stub exposing has_task + task_owner so the chain gate can resolve roles."""

    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}

    def has_task(self, task_id: str) -> bool:
        return task_id in self.tasks

    def task_owner(self, task_id: str) -> str | None:
        t = self.tasks.get(task_id)
        if t is None:
            return None
        owner = t.owner
        return owner.value if hasattr(owner, "value") else str(owner)

    async def add_task(self, task: Task) -> TaskHandle:
        self.tasks[task.task_id] = task
        return TaskHandle(task_id=task.task_id, dispatched_at=0.0)


class TestDispatchStructuralGate:
    @pytest.mark.asyncio
    async def test_behavior_without_review_dependency_refused(self, store, router) -> None:
        """A behavior task with NO review_validator dependency is refused: it can
        never satisfy the PASS precondition, so dispatch fails loud."""
        ctx = _orch_ctx(store, router)
        sched = _StubScheduler()
        # Seed an unrelated coder dependency (not a review).
        await sched.add_task(_task("t_coder", Role.CODER_WORKER))
        with pytest.raises(ValidatorChainError):
            await make_dispatch_task(ctx, scheduler=sched)(
                task_id="t_behavior",
                owner="behavior_validator",
                goal="probe",
                background="bg",
                acceptance_criteria=["f1.a1"],
                depends_on=["t_coder"],
            )
        assert "t_behavior" not in sched.tasks

    @pytest.mark.asyncio
    async def test_behavior_with_review_dependency_dispatches(self, store, router) -> None:
        """With a review_validator dependency present, the structural gate passes
        and the behavior task is admitted to the DAG (verdict checked later)."""
        ctx = _orch_ctx(store, router)
        sched = _StubScheduler()
        await sched.add_task(_task("t_review", Role.REVIEW_VALIDATOR))
        out = await make_dispatch_task(ctx, scheduler=sched)(
            task_id="t_behavior",
            owner="behavior_validator",
            goal="probe",
            background="bg",
            acceptance_criteria=["f1.a1"],
            depends_on=["t_review"],
        )
        assert out["task_id"] == "t_behavior"
        assert "t_behavior" in sched.tasks

    @pytest.mark.asyncio
    async def test_review_dependency_resolved_by_role_not_id(self, store, router) -> None:
        """The gate matches the review dependency by OWNER ROLE among several
        deps, never by a literal ID convention."""
        ctx = _orch_ctx(store, router)
        sched = _StubScheduler()
        await sched.add_task(_task("alpha", Role.CODER_WORKER))
        await sched.add_task(_task("beta", Role.REVIEW_VALIDATOR))
        await sched.add_task(_task("gamma", Role.RESEARCH_WORKER))
        out = await make_dispatch_task(ctx, scheduler=sched)(
            task_id="t_behavior",
            owner="behavior_validator",
            goal="probe",
            background="bg",
            acceptance_criteria=["f1.a1"],
            depends_on=["alpha", "beta", "gamma"],
        )
        assert out["task_id"] == "t_behavior"


# ---------------------------------------------------------------------------
# Layer 2 — Scheduler runtime verdict gate (condition (b))
# ---------------------------------------------------------------------------


class _RecordingAgent(BaseAgent[str]):
    """Agent that records every task_id it runs and writes a verdict for its role.

    review_validator → writes verdicts/<task_id>.review.json (result = review_result)
    behavior_validator → writes verdicts/<task_id>.behavior.json (result = behavior_result)
    """

    role = Role.REVIEW_VALIDATOR
    prompt_path = Path("prompts/coder_worker.md")

    def __init__(
        self,
        *,
        store,
        event_log,
        router,
        sandbox,
        role: Role,
        review_result: VerdictResult = VerdictResult.PASS,
        behavior_result: VerdictResult = VerdictResult.PASS,
        ran: list[str],
    ) -> None:
        self.role = role  # type: ignore[assignment]
        super().__init__(store=store, event_log=event_log, router=router, sandbox=sandbox)
        self._review_result = review_result
        self._behavior_result = behavior_result
        self._ran = ran

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
        self._ran.append(task.task_id)
        if self.role == Role.REVIEW_VALIDATOR:
            self.store.save_review_verdict(task.task_id, _review_verdict(task.task_id, self._review_result))
        elif self.role == Role.BEHAVIOR_VALIDATOR:
            from maf_coder.schemas import BehaviorVerdict

            self.store.save_behavior_verdict(
                task.task_id,
                BehaviorVerdict(
                    task_id=task.task_id,
                    result=self._behavior_result,
                    probe_strategy="cli",
                    evidence_path=f"behavior_evidence/{task.task_id}",
                    failure_reason=(
                        None if self._behavior_result == VerdictResult.PASS else "probe mismatch"
                    ),
                ),
            )
        return AgentResult(
            role=self.role,
            task_id=task.task_id,
            parsed_output="ok",
            raw_output="",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_sec=0.0,
            model_used="openai/x",
            fallback_used=False,
            tools_invoked=[],
            errored=False,
            error_reason=None,
        )


async def _run_chain(
    *,
    tmp_path: Path,
    store: ArtifactStore,
    router: ModelRouter,
    review_result: VerdictResult,
    behavior_result: VerdictResult = VerdictResult.PASS,
) -> tuple[Scheduler, list[str]]:
    """Build a review→behavior DAG and run it to completion."""
    sandbox = LocalShellSandbox()
    await sandbox.start(workspace_mount=tmp_path / "ws")
    ran: list[str] = []
    try:
        def make_agent(role: Role) -> _RecordingAgent:
            return _RecordingAgent(
                store=store,
                event_log=store.event_log(),
                router=router,
                sandbox=sandbox,
                role=role,
                review_result=review_result,
                behavior_result=behavior_result,
                ran=ran,
            )

        sched = Scheduler(
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
            agent_factory={
                Role.REVIEW_VALIDATOR: lambda: make_agent(Role.REVIEW_VALIDATOR),
                Role.BEHAVIOR_VALIDATOR: lambda: make_agent(Role.BEHAVIOR_VALIDATOR),
            },
            mission_id="m-chain",
        )
        await sched.add_task(_task("t_review", Role.REVIEW_VALIDATOR))
        await sched.add_task(
            _task("t_behavior", Role.BEHAVIOR_VALIDATOR, depends_on=["t_review"])
        )
        await sched.run()
        return sched, ran
    finally:
        await sandbox.stop()


class TestSchedulerVerdictGate:
    @pytest.mark.asyncio
    async def test_review_fail_blocks_behavior(self, tmp_path, store, router) -> None:
        """Review FAIL ⇒ behavior task never executes (blocked) and a
        validator_chain_blocked event is emitted."""
        sched, ran = await _run_chain(
            tmp_path=tmp_path, store=store, router=router, review_result=VerdictResult.FAIL
        )
        # State: review completed, behavior blocked.
        assert sched.task_status("t_review") == "complete"
        assert sched.task_status("t_behavior") == "blocked"
        # Behavior agent never ran.
        assert "t_behavior" not in ran
        # Event emitted with the implementation_path_issue signal.
        blocked = [
            e for e in store.event_log().iter_events() if e.kind == "validator_chain_blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0].task_id == "t_behavior"
        assert blocked[0].payload["review_task_id"] == "t_review"
        assert blocked[0].payload["signal"] == "implementation_path_issue"

    @pytest.mark.asyncio
    async def test_review_pass_allows_behavior(self, tmp_path, store, router) -> None:
        """Review PASS ⇒ behavior task is dispatchable and runs to completion."""
        sched, ran = await _run_chain(
            tmp_path=tmp_path, store=store, router=router, review_result=VerdictResult.PASS
        )
        assert sched.task_status("t_review") == "complete"
        assert sched.task_status("t_behavior") == "complete"
        assert ran == ["t_review", "t_behavior"]
        # No chain-blocked event when the gate is satisfied.
        blocked = [
            e for e in store.event_log().iter_events() if e.kind == "validator_chain_blocked"
        ]
        assert blocked == []

    @pytest.mark.asyncio
    async def test_behavior_fail_verdict_is_observable(self, tmp_path, store, router) -> None:
        """Behavior FAIL (downstream of a review PASS) ⇒ the behavior verdict is
        written and observable as FAIL.

        D4 hook: PR-D4 arbitration will read this FAIL verdict and emit the
        'implementation path issue' re-plan signal (Review PASS + Behavior FAIL
        row of the arbitration table). D3 only guarantees the FAIL verdict is
        produced and observable; arbitration belongs to D4. See execution plan
        §2 PR-D4.
        """
        sched, ran = await _run_chain(
            tmp_path=tmp_path,
            store=store,
            router=router,
            review_result=VerdictResult.PASS,
            behavior_result=VerdictResult.FAIL,
        )
        # Behavior task ran (review gate satisfied) and produced a FAIL verdict.
        assert sched.task_status("t_behavior") == "complete"
        assert "t_behavior" in ran
        verdict = store.load_behavior_verdict("t_behavior")
        assert verdict.result == VerdictResult.FAIL.value
        assert verdict.failure_reason is not None
