"""Orchestrator tool tests (AGENT_TOOLS_SPEC §6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from maf_coder.agents.base import TaskContext
from maf_coder.agents.errors import (
    ArtifactError,
    AssertionUnknownError,
    PermissionDeniedError,
    TaskAlreadyDispatchedError,
)
from maf_coder.agents.results import TaskHandle
from maf_coder.agents.tools.orchestrator_tools import (
    build_orchestrator_tools,
    make_create_pr,
    make_dispatch_task,
    make_emit_event,
    make_escalate_to_human_gate,
    make_get_budget_status,
    make_get_mission_state,
    make_mark_user_message_processed,
    make_poll_user_messages,
    make_read_artifact,
    make_save_artifact,
    make_update_mission_state,
)
from maf_coder.blackboard import ArtifactStore
from maf_coder.models.router import ModelRouter
from maf_coder.schemas import (
    Assertion,
    Feature,
    MissionState,
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
    s = ArtifactStore(tmp_path / "missions", "m-orch")
    contract = ValidationContract(
        mission_id="m-orch",
        features=[
            Feature(
                feature_id="f1",
                description="health",
                assertions=[
                    Assertion(
                        id="f1.a1",
                        statement="GET /health returns 200",
                        verification_method=VerificationMethod.INTEGRATION_TEST,
                        verification_target="tests/health.rs::test_ok",
                    )
                ],
            )
        ],
    )
    s.save_validation_contract(contract)
    ms = MissionState(
        mission_id="m-orch",
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    s.save_mission_state(ms)
    return s


class _StubSandbox:
    async def exec(self, cmd: str, *, cwd: str = "/workspace", timeout_sec: int = 60):
        from maf_coder.agents.results import CommandResult

        return CommandResult(command=cmd, exit_code=0, stdout="", stderr="", duration_sec=0.0)

    async def commit_snapshot(self, *, image_tag: str) -> str:
        return f"sha256:{image_tag}"


def _orch_ctx(store: ArtifactStore, router: ModelRouter, sandbox: Any) -> TaskContext:
    task = Task(
        task_id="orch-t1",
        parent_milestone="m1",
        owner=Role.ORCHESTRATOR,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="x",
        background="x",
        acceptance_criteria=[],
        required_outputs=["plan.md"],
        permission=Permission(allowed_paths=["**"], network_policy=NetworkPolicy.NONE),
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=60),
    )
    return TaskContext(
        task=task,
        mission_id="m-orch",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
    )


def _coder_ctx(store: ArtifactStore, router: ModelRouter, sandbox: Any) -> TaskContext:
    task = Task(
        task_id="c-t1",
        parent_milestone="m1",
        owner=Role.CODER_WORKER,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="x",
        background="x",
        acceptance_criteria=["f1.a1"],
        required_outputs=["x"],
        permission=Permission(allowed_paths=["**"], network_policy=NetworkPolicy.NONE),
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=60),
    )
    return TaskContext(
        task=task,
        mission_id="m-orch",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
    )


class _StubScheduler:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}

    def has_task(self, task_id: str) -> bool:
        return task_id in self.tasks

    async def add_task(self, task: Task) -> TaskHandle:
        self.tasks[task.task_id] = task
        return TaskHandle(task_id=task.task_id, dispatched_at=0.0)


class TestDispatchTask:
    @pytest.mark.asyncio
    async def test_orchestrator_only(self, store, router) -> None:
        coder_ctx = _coder_ctx(store, router, _StubSandbox())
        with pytest.raises(PermissionDeniedError):
            await make_dispatch_task(coder_ctx)(
                task_id="t2",
                owner="coder_worker",
                goal="x",
                background="x",
                acceptance_criteria=["f1.a1"],
            )

    @pytest.mark.asyncio
    async def test_unknown_assertion_rejected(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        with pytest.raises(AssertionUnknownError):
            await make_dispatch_task(ctx)(
                task_id="t2",
                owner="coder_worker",
                goal="x",
                background="x",
                acceptance_criteria=["does_not_exist"],
            )

    @pytest.mark.asyncio
    async def test_dispatch_via_scheduler(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        sched = _StubScheduler()
        out = await make_dispatch_task(ctx, scheduler=sched)(
            task_id="t2",
            owner="coder_worker",
            goal="impl health",
            background="bg",
            acceptance_criteria=["f1.a1"],
        )
        assert out["task_id"] == "t2"
        assert "t2" in sched.tasks

    @pytest.mark.asyncio
    async def test_milestone_precedence_explicit_then_current_then_inherit(
        self, store, router
    ) -> None:
        """Milestone precedence: explicit milestone_id > live current_milestone >
        the Orchestrator turn's snapshot (_orch_ctx task uses 'm1')."""
        ctx = _orch_ctx(store, router, _StubSandbox())
        sched = _StubScheduler()

        # current_milestone unset (None) → falls back to the turn's milestone 'm1'.
        await make_dispatch_task(ctx, scheduler=sched)(
            task_id="t-inherit",
            owner="coder_worker",
            goal="x",
            background="x",
            acceptance_criteria=["f1.a1"],
        )
        # Set the live current_milestone → the default now follows it.
        ms = store.load_mission_state()
        store.save_mission_state(ms.model_copy(update={"current_milestone": "live-ms"}))
        await make_dispatch_task(ctx, scheduler=sched)(
            task_id="t-current",
            owner="coder_worker",
            goal="x",
            background="x",
            acceptance_criteria=["f1.a1"],
        )
        # Explicit milestone_id wins over current_milestone.
        await make_dispatch_task(ctx, scheduler=sched)(
            task_id="t-explicit",
            owner="coder_worker",
            goal="x",
            background="x",
            acceptance_criteria=["f1.a1"],
            milestone_id="m3",
        )
        assert sched.tasks["t-inherit"].parent_milestone == "m1"
        assert sched.tasks["t-current"].parent_milestone == "live-ms"
        assert sched.tasks["t-explicit"].parent_milestone == "m3"

    @pytest.mark.asyncio
    async def test_duplicate_dispatch_raises(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        sched = _StubScheduler()
        await make_dispatch_task(ctx, scheduler=sched)(
            task_id="t2",
            owner="coder_worker",
            goal="x",
            background="x",
            acceptance_criteria=["f1.a1"],
        )
        with pytest.raises(TaskAlreadyDispatchedError):
            await make_dispatch_task(ctx, scheduler=sched)(
                task_id="t2",
                owner="coder_worker",
                goal="x",
                background="x",
                acceptance_criteria=["f1.a1"],
            )

    @pytest.mark.asyncio
    async def test_dispatch_emits_event_when_no_scheduler(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        await make_dispatch_task(ctx)(
            task_id="t9",
            owner="coder_worker",
            goal="x",
            background="x",
            acceptance_criteria=["f1.a1"],
        )
        kinds = [e.kind for e in ctx.event_log.iter_events()]
        assert "task_dispatched" in kinds


class TestArtifactTools:
    @pytest.mark.asyncio
    async def test_read_artifact(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        store.write_text("plan.md", "# plan\n")
        content = await make_read_artifact(ctx)(path="plan.md")
        assert content.startswith("# plan")

    @pytest.mark.asyncio
    async def test_read_artifact_missing(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        with pytest.raises(ArtifactError):
            await make_read_artifact(ctx)(path="nope.md")

    @pytest.mark.asyncio
    async def test_save_artifact_allowed_path(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        await make_save_artifact(ctx)(path="plan.md", content="# new plan\n")
        assert store.read_text("plan.md").startswith("# new plan")

    @pytest.mark.asyncio
    async def test_save_artifact_disallowed_path(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        with pytest.raises(PermissionDeniedError):
            await make_save_artifact(ctx)(path="handoff/x.json", content="{}")

    @pytest.mark.asyncio
    async def test_save_artifact_contract_locked(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        # validation_contract.yaml already exists from the fixture
        from maf_coder.blackboard.artifact_store import ContractAlreadyLockedError

        with pytest.raises(ContractAlreadyLockedError):
            await make_save_artifact(ctx)(path="validation_contract.yaml", content="x")


class TestEmitAndEscalate:
    @pytest.mark.asyncio
    async def test_emit_event(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        await make_emit_event(ctx)(kind="custom_decision", payload={"x": 1})
        kinds = [e.kind for e in ctx.event_log.iter_events()]
        assert "custom_decision" in kinds

    @pytest.mark.asyncio
    async def test_escalate_writes_pending(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        await make_escalate_to_human_gate(ctx)(
            reason="ambiguous_choice",
            options=["a", "b"],
            recommendation="a",
        )
        pending = [
            p.name for p in store.list_dir("user_messages") if p.name.startswith("_pending_")
        ]
        assert len(pending) == 1
        kinds = [e.kind for e in ctx.event_log.iter_events()]
        assert "escalation_triggered" in kinds


class TestUserMessages:
    @pytest.mark.asyncio
    async def test_poll_then_mark(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        store.write_text("user_messages/note.md", "hi\n")
        store.write_text("user_messages/!urgent-fix.md", "stop\n")
        out = await make_poll_user_messages(ctx)()
        assert len(out) == 2
        assert out[0]["urgent"] is True  # urgent first
        await make_mark_user_message_processed(ctx)(filename="note.md")
        # original gone, processed copy exists
        assert not store.exists("user_messages/note.md")
        assert store.exists("processed_messages/note.md")


class TestMissionState:
    @pytest.mark.asyncio
    async def test_get_returns_dict(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        out = await make_get_mission_state(ctx)()
        assert out["mission_id"] == "m-orch"

    @pytest.mark.asyncio
    async def test_update_patchable_keys(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        out = await make_update_mission_state(ctx)(
            updates={"current_milestone": "m1", "coder_provider_in_use": "anthropic"}
        )
        assert out["current_milestone"] == "m1"
        assert out["coder_provider_in_use"] == "anthropic"

    @pytest.mark.asyncio
    async def test_update_blocks_framework_keys(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        with pytest.raises(PermissionDeniedError):
            await make_update_mission_state(ctx)(updates={"cumulative_cost_usd": 99.0})


class TestCompleteMission:
    @pytest.mark.asyncio
    async def test_sets_mission_complete_flag(self, store, router) -> None:
        from maf_coder.agents.tools.orchestrator_tools import make_complete_mission

        ctx = _orch_ctx(store, router, _StubSandbox())
        assert store.load_mission_state().mission_complete is False
        out = await make_complete_mission(ctx)(summary="goal delivered")
        assert out["mission_complete"] is True
        assert store.load_mission_state().mission_complete is True
        assert "complete_mission" in ctx.tools_invoked


class TestBudgetStatus:
    @pytest.mark.asyncio
    async def test_zero_when_no_calls(self, store, router) -> None:
        ctx = _orch_ctx(store, router, _StubSandbox())
        out = await make_get_budget_status(ctx)()
        assert out["cost_usd"] == 0.0
        assert out["current_mode"] == "normal"


class TestCreateCheckpoint:
    @pytest.mark.asyncio
    async def test_checkpoint_with_stub_sandbox(self, store, router) -> None:
        from maf_coder.agents.tools.orchestrator_tools import make_create_checkpoint

        sandbox = _StubSandbox()
        ctx = _orch_ctx(store, router, sandbox)
        out = await make_create_checkpoint(ctx)(milestone_id="m1")
        assert out["milestone_id"] == "m1"
        assert out["git_tag"].startswith("mission/m-orch/")
        assert store.exists("checkpoints/m1/checkpoint.json")
        kinds = [e.kind for e in ctx.event_log.iter_events()]
        assert "checkpoint_created" in kinds


def test_factory_list_completeness(store, router) -> None:
    ctx = _orch_ctx(store, router, _StubSandbox())
    tools = build_orchestrator_tools(ctx)
    names = {t.__name__ for t in tools}
    for n in (
        "dispatch_task",
        "read_artifact",
        "save_artifact",
        "emit_event",
        "escalate_to_human_gate",
        "create_checkpoint",
        "poll_user_messages",
        "mark_user_message_processed",
        "get_mission_state",
        "update_mission_state",
        "complete_mission",
        "get_budget_status",
        "create_pr",  # F-pr
    ):
        assert n in names, f"missing orch tool: {n}"


# ---------------------------------------------------------------------------
# create_pr  (F-pr: PR workflow)
# ---------------------------------------------------------------------------


class _PrStubSandbox:
    """Records exec calls; returns gitleaks-clean + a canned PR URL."""

    def __init__(self, *, dirty: bool = False) -> None:
        self.dirty = dirty
        self.calls: list[str] = []

    async def exec(self, cmd: str, *, cwd: str = "/workspace", timeout_sec: int = 60):
        from maf_coder.agents.results import CommandResult

        self.calls.append(cmd)
        if "gitleaks" in cmd:
            if self.dirty:
                import json as _json

                payload = _json.dumps([{"File": "src/main.rs", "Description": "key"}])
                return CommandResult(
                    command=cmd, exit_code=1, stdout=payload, stderr="", duration_sec=0.0
                )
            return CommandResult(command=cmd, exit_code=0, stdout="[]", stderr="", duration_sec=0.0)
        return CommandResult(
            command=cmd,
            exit_code=0,
            stdout="https://github.com/acme/widget/pull/9",
            stderr="",
            duration_sec=0.0,
        )


class TestCreatePrTool:
    @pytest.mark.asyncio
    async def test_happy_path_creates_pr(self, store, router) -> None:
        store.write_text("plan.md", "# Goal\n")
        sandbox = _PrStubSandbox()
        ctx = _orch_ctx(store, router, sandbox)
        result = await make_create_pr(ctx)(
            repo_path="/workspace", head_branch="feature/x"
        )
        assert result["created"] is True
        assert result["url"] == "https://github.com/acme/widget/pull/9"
        assert result["refused"] is False
        assert any(c.startswith("gh pr create") for c in sandbox.calls)
        assert "create_pr" in ctx.tools_invoked

    @pytest.mark.asyncio
    async def test_dirty_refuses(self, store, router) -> None:
        sandbox = _PrStubSandbox(dirty=True)
        ctx = _orch_ctx(store, router, sandbox)
        result = await make_create_pr(ctx)(
            repo_path="/workspace", head_branch="feature/x"
        )
        assert result["created"] is False
        assert result["refused"] is True
        assert len(result["gitleaks_findings"]) == 1
        assert not any(c.startswith("gh pr create") for c in sandbox.calls)

    @pytest.mark.asyncio
    async def test_non_orchestrator_denied(self, store, router) -> None:
        sandbox = _PrStubSandbox()
        ctx = _coder_ctx(store, router, sandbox)
        with pytest.raises(PermissionDeniedError):
            await make_create_pr(ctx)(repo_path="/workspace", head_branch="feature/x")

    @pytest.mark.asyncio
    async def test_invalid_provider(self, store, router) -> None:
        sandbox = _PrStubSandbox()
        ctx = _orch_ctx(store, router, sandbox)
        with pytest.raises(ArtifactError):
            await make_create_pr(ctx)(
                repo_path="/workspace", head_branch="feature/x", provider="bitbucket"
            )
