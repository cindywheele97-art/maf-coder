"""BehaviorValidator tool factory tests (AGENT_TOOLS_SPEC §11, Phase D PR-D1).

Exercises the six §11 tools + the aggregator against a real LocalShellSandbox.
HTTP probes are driven with a stubbed sandbox.exec so the suite needs no live
server; CLI probes and service lifecycle use shell builtins.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from maf_coder.agents.base import TaskContext
from maf_coder.agents.errors import ArtifactError, PermissionDeniedError, ToolError
from maf_coder.agents.results import CommandResult
from maf_coder.agents.tools.behavior_tools import (
    build_behavior_tools,
    make_probe_cli,
    make_probe_http,
    make_save_behavior_evidence,
    make_save_behavior_verdict,
    make_start_service,
    make_stop_service,
)
from maf_coder.blackboard import ArtifactStore
from maf_coder.blackboard.event_log import EventKind
from maf_coder.models.router import ModelRouter
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import (
    NetworkPolicy,
    Permission,
    RiskLevel,
    Role,
    Task,
    TaskBudget,
    VerdictResult,
)

MISSION = "m-behavior-tools"


@pytest.fixture
def router(tmp_path: Path) -> ModelRouter:
    cfg = tmp_path / "droid.yaml"
    cfg.write_text(
        "version: 1\n"
        "roles:\n"
        "  behavior_validator:\n"
        "    primary:\n"
        "      model: google/x\n"
        "      temperature: 0.0\n"
        "      max_tokens: 1000\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", MISSION)


@pytest.fixture
async def sandbox(tmp_path: Path) -> AsyncIterator[LocalShellSandbox]:
    sb = LocalShellSandbox()
    await sb.start(workspace_mount=tmp_path / "ws")
    try:
        yield sb
    finally:
        await sb.stop()


def _ctx(
    sandbox: LocalShellSandbox,
    store: ArtifactStore,
    router: ModelRouter,
    *,
    permission: Permission | None = None,
    task_id: str = "t-behavior",
) -> TaskContext:
    perm = permission or Permission(
        allowed_paths=["**"], allowed_tools=[], network_policy=NetworkPolicy.NONE
    )
    task = Task(
        task_id=task_id,
        parent_milestone="m1",
        owner=Role.BEHAVIOR_VALIDATOR,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="probe behavior",
        background="b",
        acceptance_criteria=["f1.a1"],
        required_outputs=[f"verdicts/{task_id}.behavior.json"],
        permission=perm,
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=60),
    )
    return TaskContext(
        task=task,
        mission_id=MISSION,
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
    )


# ---------------------------------------------------------------------------
# Builder + permissions
# ---------------------------------------------------------------------------


class TestBuilder:
    def test_builds_six_plus_runner(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        tools = build_behavior_tools(ctx)
        # The six §11 tools plus the probe runner aggregator.
        assert len(tools) == 7
        for t in tools:
            assert callable(t)


class TestPermissions:
    @pytest.mark.asyncio
    async def test_probe_cli_denied_when_not_allowed(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        perm = Permission(
            allowed_paths=["**"],
            allowed_tools=["save_behavior_verdict"],  # probe_cli not listed
            network_policy=NetworkPolicy.NONE,
        )
        ctx = _ctx(sandbox, store, router, permission=perm)
        with pytest.raises(PermissionDeniedError):
            await make_probe_cli(ctx)(binary="true")


# ---------------------------------------------------------------------------
# probe_cli
# ---------------------------------------------------------------------------


class TestProbeCli:
    @pytest.mark.asyncio
    async def test_matches_zero_exit(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        out = await make_probe_cli(ctx)(binary="true")
        assert out["exit_code"] == 0
        assert out["matched"] is True

    @pytest.mark.asyncio
    async def test_expected_exit_code_mismatch(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        out = await make_probe_cli(ctx)(binary="false", expected_exit_code=0)
        assert out["exit_code"] != 0
        assert out["matched"] is False


# ---------------------------------------------------------------------------
# probe_http (stubbed sandbox.exec)
# ---------------------------------------------------------------------------


class TestProbeHttp:
    @pytest.mark.asyncio
    async def test_status_parsed_and_matched(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_exec(cmd: str, **_: object) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=0, stdout="200", stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        out = await make_probe_http(ctx)(url="http://localhost:8080/health", expected_status=200)
        assert out["status_code"] == 200
        assert out["matched"] is True

    @pytest.mark.asyncio
    async def test_status_mismatch(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_exec(cmd: str, **_: object) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=0, stdout="500", stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        out = await make_probe_http(ctx)(url="http://localhost:8080/health", expected_status=200)
        assert out["status_code"] == 500
        assert out["matched"] is False


# ---------------------------------------------------------------------------
# start_service / stop_service
# ---------------------------------------------------------------------------


class TestServiceLifecycle:
    @pytest.mark.asyncio
    async def test_start_then_stop(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        info = await make_start_service(ctx)(
            command="sh -c 'touch ready; sleep 30'",
            ready_check="test -f ready",
            timeout_sec=5,
        )
        assert info["service_id"].startswith("svc-")
        assert "log_path" in info
        # Stopping a known service must succeed.
        await make_stop_service(ctx)(service_id=info["service_id"])

    @pytest.mark.asyncio
    async def test_start_never_ready_raises(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(Exception):  # SandboxError (subclass of ToolError)
            await make_start_service(ctx)(
                command="sh -c 'exit 1'",
                ready_check="false",
                timeout_sec=1,
            )

    @pytest.mark.asyncio
    async def test_stop_unknown_service_raises(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(ToolError):
            await make_stop_service(ctx)(service_id="svc-nope")


# ---------------------------------------------------------------------------
# save_behavior_evidence / save_behavior_verdict
# ---------------------------------------------------------------------------


class TestSaveEvidence:
    @pytest.mark.asyncio
    async def test_writes_under_task_dir(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        path = await make_save_behavior_evidence(ctx)(
            task_id="t-behavior", name="trace.log", content=b"hello evidence"
        )
        assert path == "behavior_evidence/t-behavior/trace.log"
        assert store.read_text(path) == "hello evidence"
        kinds = {e.kind for e in ctx.event_log.iter_events()}
        assert EventKind.ARTIFACT_WRITTEN.value in kinds

    @pytest.mark.asyncio
    async def test_path_escape_rejected(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(ArtifactError):
            await make_save_behavior_evidence(ctx)(
                task_id="../../etc", name="passwd", content=b"x"
            )


class TestSaveVerdict:
    @pytest.mark.asyncio
    async def test_round_trip_pass(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        path = await make_save_behavior_verdict(ctx)(
            task_id="t-behavior",
            result="pass",
            probe_strategy="cli_assert_cmd_probe",
            observations=[
                {
                    "assertion_id": "f1.a1",
                    "observed": "exit_code=0",
                    "expected": "exit_code=0",
                    "matched": True,
                }
            ],
            evidence_path="",
        )
        assert "t-behavior.behavior.json" in path
        loaded = store.load_behavior_verdict("t-behavior")
        assert loaded.result == VerdictResult.PASS.value
        assert len(loaded.observations) == 1
        kinds = {e.kind for e in ctx.event_log.iter_events()}
        assert EventKind.VALIDATOR_VERDICT.value in kinds

    @pytest.mark.asyncio
    async def test_round_trip_fail_with_evidence_path(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        await make_save_behavior_verdict(ctx)(
            task_id="t-behavior",
            result="fail",
            probe_strategy="backend_service_health_probe",
            observations=[
                {
                    "assertion_id": "f1.a1",
                    "observed": "status=500",
                    "expected": "status=200",
                    "matched": False,
                }
            ],
            evidence_path="behavior_evidence/t-behavior",
            failure_reason="endpoint returned 500",
        )
        loaded = store.load_behavior_verdict("t-behavior")
        assert loaded.result == VerdictResult.FAIL.value
        assert loaded.failure_reason == "endpoint returned 500"
        assert loaded.evidence_path == "behavior_evidence/t-behavior"

    @pytest.mark.asyncio
    async def test_invalid_observation_raises(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(ToolError):
            await make_save_behavior_verdict(ctx)(
                task_id="t-behavior",
                result="pass",
                probe_strategy="cli_assert_cmd_probe",
                observations=[{"assertion_id": "f1.a1"}],  # missing required fields
            )
