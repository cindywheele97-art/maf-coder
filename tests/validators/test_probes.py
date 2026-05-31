"""Probe strategy tests (Phase D PR-D1).

Each strategy is driven against a real `LocalShellSandbox` (never the host
shell directly) with mock binaries/services expressed as shell commands. cli /
backend / library get a pass-path AND a fail-path; on the fail path we assert
the strategy produced evidence and that, once routed through the runner, the
evidence file actually lands on disk. embedded / wasm get one build-only smoke
test each.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from maf_coder.agents.base import TaskContext
from maf_coder.agents.tools.behavior_tools import run_behavior_probes_impl
from maf_coder.blackboard import ArtifactStore
from maf_coder.models.router import ModelRouter
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import (
    Assertion,
    BehaviorProbeSpec,
    Crate,
    Feature,
    NetworkPolicy,
    Permission,
    ProjectProfile,
    ProjectType,
    RiskLevel,
    Role,
    Task,
    TaskBudget,
    ValidationContract,
    VerdictResult,
    VerificationMethod,
)
from maf_coder.validators.probes import (
    BackendServiceHealthProbe,
    CliAssertCmdProbe,
    EmbeddedHostTestProbe,
    LibraryExampleProbe,
    WasmNodeProbe,
    get_probe_strategy,
    known_strategies,
)
from maf_coder.validators.probes import backend as backend_mod

MISSION = "m-behavior"


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
    task_id: str = "t-behavior",
) -> TaskContext:
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
        permission=Permission(
            allowed_paths=["**"], allowed_tools=[], network_policy=NetworkPolicy.NONE
        ),
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


def _assertions(*ids: str) -> list[Assertion]:
    return [
        Assertion(
            id=i,
            statement=f"behavior {i} holds",
            verification_method=VerificationMethod.BEHAVIOR_PROBE,
            verification_target=f"behavior_probe::{i}",
        )
        for i in ids
    ]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_all_five_strategies_registered(self) -> None:
        assert known_strategies() == sorted(
            [
                "backend_service_health_probe",
                "cli_assert_cmd_probe",
                "library_example_probe",
                "embedded_host_test_probe",
                "wasm_node_probe",
            ]
        )

    def test_resolves_concrete_classes(self) -> None:
        assert isinstance(get_probe_strategy("cli_assert_cmd_probe"), CliAssertCmdProbe)
        assert isinstance(
            get_probe_strategy("backend_service_health_probe"), BackendServiceHealthProbe
        )

    def test_unknown_strategy_raises(self) -> None:
        with pytest.raises(KeyError):
            get_probe_strategy("nope")


# ---------------------------------------------------------------------------
# CLI probe
# ---------------------------------------------------------------------------


class TestCliProbe:
    @pytest.mark.asyncio
    async def test_pass_path(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        spec = BehaviorProbeSpec(strategy="cli_assert_cmd_probe", endpoints_to_probe=["true"])
        result = await CliAssertCmdProbe().run(ctx, spec, _assertions("f1.a1"))
        assert result.passed is True
        assert len(result.observations) == 1
        assert result.observations[0].matched is True
        assert result.evidence == {}

    @pytest.mark.asyncio
    async def test_fail_path_produces_evidence(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        spec = BehaviorProbeSpec(
            strategy="cli_assert_cmd_probe",
            endpoints_to_probe=["sh -c 'echo boom 1>&2; exit 3'"],
        )
        result = await CliAssertCmdProbe().run(ctx, spec, _assertions("f1.a1"))
        assert result.passed is False
        assert result.observations[0].matched is False
        # Strategy captured stdout+stderr evidence for the failed assertion.
        assert "f1.a1.stderr.txt" in result.evidence


# ---------------------------------------------------------------------------
# Backend probe
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill poll delays so backend ready-waits don't slow the suite."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(backend_mod, "_async_sleep", _instant)


class TestBackendProbe:
    @pytest.mark.asyncio
    async def test_pass_path(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        # start_command drops a 'ready' marker; ready_check polls for it;
        # the endpoint probe checks the same marker.
        spec = BehaviorProbeSpec(
            strategy="backend_service_health_probe",
            start_command="sh -c 'touch ready; sleep 30'",
            ready_check="test -f ready",
            endpoints_to_probe=["test -f ready"],
            timeout_sec=5,
        )
        result = await BackendServiceHealthProbe().run(ctx, spec, _assertions("f1.a1"))
        assert result.passed is True
        assert result.observations[0].matched is True

    @pytest.mark.asyncio
    async def test_fail_path_service_never_ready_writes_log(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        spec = BehaviorProbeSpec(
            strategy="backend_service_health_probe",
            start_command="sh -c 'echo startup-failed 1>&2; exit 1'",
            ready_check="false",  # never ready
            endpoints_to_probe=["true"],
            timeout_sec=1,
        )
        result = await BackendServiceHealthProbe().run(ctx, spec, _assertions("f1.a1"))
        assert result.passed is False
        assert result.observations[0].matched is False
        assert "service.log" in result.evidence

    @pytest.mark.asyncio
    async def test_missing_start_command_fails_cleanly(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        spec = BehaviorProbeSpec(strategy="backend_service_health_probe")
        result = await BackendServiceHealthProbe().run(ctx, spec, _assertions("f1.a1"))
        assert result.passed is False
        assert result.failure_reason is not None


# ---------------------------------------------------------------------------
# Library probe
# ---------------------------------------------------------------------------


class TestLibraryProbe:
    @pytest.mark.asyncio
    async def test_pass_path(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        spec = BehaviorProbeSpec(
            strategy="library_example_probe", endpoints_to_probe=["true"]
        )
        result = await LibraryExampleProbe().run(ctx, spec, _assertions("f1.a1"))
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_fail_path_produces_evidence(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        spec = BehaviorProbeSpec(
            strategy="library_example_probe", endpoints_to_probe=["false"]
        )
        result = await LibraryExampleProbe().run(ctx, spec, _assertions("f1.a1"))
        assert result.passed is False
        assert "f1.a1.stderr.txt" in result.evidence


# ---------------------------------------------------------------------------
# Embedded + WASM smoke (build-only)
# ---------------------------------------------------------------------------


class TestEmbeddedSmoke:
    @pytest.mark.asyncio
    async def test_host_test_smoke(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        spec = BehaviorProbeSpec(
            strategy="embedded_host_test_probe", endpoints_to_probe=["true"]
        )
        result = await EmbeddedHostTestProbe().run(ctx, spec, _assertions("f1.a1"))
        assert result.passed is True
        assert result.observations[0].assertion_id == "f1.a1"


class TestWasmSmoke:
    @pytest.mark.asyncio
    async def test_build_pack_smoke(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Substitute the cargo/wasm-pack build commands with always-succeed
        # shell so the smoke test doesn't require a Rust toolchain.
        monkeypatch.setattr(backend_mod, "_async_sleep", lambda *_: None, raising=False)
        from maf_coder.validators.probes import wasm as wasm_mod

        monkeypatch.setattr(wasm_mod, "_BUILD_CMD", "true")
        monkeypatch.setattr(wasm_mod, "_PACK_CMD", "true")
        ctx = _ctx(sandbox, store, router)
        spec = BehaviorProbeSpec(strategy="wasm_node_probe")
        result = await WasmNodeProbe().run(ctx, spec, _assertions("f1.a1"))
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_build_fail_writes_evidence(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from maf_coder.validators.probes import wasm as wasm_mod

        monkeypatch.setattr(wasm_mod, "_BUILD_CMD", "false")
        ctx = _ctx(sandbox, store, router)
        spec = BehaviorProbeSpec(strategy="wasm_node_probe")
        result = await WasmNodeProbe().run(ctx, spec, _assertions("f1.a1"))
        assert result.passed is False
        assert "build.stderr.txt" in result.evidence


# ---------------------------------------------------------------------------
# Probe runner — wiring profile + contract -> verdict round-trip
# ---------------------------------------------------------------------------


def _seed_profile_and_contract(
    store: ArtifactStore, spec: BehaviorProbeSpec, assertion_ids: list[str]
) -> None:
    profile = ProjectProfile(
        project_type=ProjectType.CLI,
        crate_layout="single",
        crates=[Crate(name="cli", type="binary", targets=["cli"])],
        behavior_probe=spec,
    )
    store.save_project_profile(profile)
    contract = ValidationContract(
        mission_id=MISSION,
        features=[
            Feature(
                feature_id="f1",
                description="behavior feature",
                assertions=[
                    Assertion(
                        id=i,
                        statement=f"behavior {i}",
                        verification_method=VerificationMethod.BEHAVIOR_PROBE,
                        verification_target=f"behavior_probe::{i}",
                    )
                    for i in assertion_ids
                ],
            )
        ],
    )
    store.save_validation_contract(contract)


class TestProbeRunner:
    @pytest.mark.asyncio
    async def test_pass_round_trips_verdict(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        spec = BehaviorProbeSpec(strategy="cli_assert_cmd_probe", endpoints_to_probe=["true"])
        _seed_profile_and_contract(store, spec, ["f1.a1"])
        ctx = _ctx(sandbox, store, router)

        out = await run_behavior_probes_impl(ctx, "t-behavior")
        assert out["result"] == VerdictResult.PASS.value
        assert out["evidence_path"] == ""

        loaded = store.load_behavior_verdict("t-behavior")
        assert loaded.result == VerdictResult.PASS.value
        assert loaded.probe_strategy == "cli_assert_cmd_probe"
        assert len(loaded.observations) == 1
        assert loaded.observations[0].assertion_id == "f1.a1"

    @pytest.mark.asyncio
    async def test_fail_writes_evidence_before_returning(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        spec = BehaviorProbeSpec(strategy="cli_assert_cmd_probe", endpoints_to_probe=["false"])
        _seed_profile_and_contract(store, spec, ["f1.a1"])
        ctx = _ctx(sandbox, store, router)

        out = await run_behavior_probes_impl(ctx, "t-behavior")
        assert out["result"] == VerdictResult.FAIL.value
        assert out["evidence_path"] == "behavior_evidence/t-behavior"

        # Hard exit-gate: evidence on disk BEFORE the verdict is consumable.
        assert store.exists("behavior_evidence/t-behavior/f1.a1.stderr.txt")
        loaded = store.load_behavior_verdict("t-behavior")
        assert loaded.result == VerdictResult.FAIL.value
        assert loaded.evidence_path == "behavior_evidence/t-behavior"
        assert loaded.failure_reason is not None

    @pytest.mark.asyncio
    async def test_one_to_one_observations_for_multiple_assertions(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        spec = BehaviorProbeSpec(
            strategy="cli_assert_cmd_probe", endpoints_to_probe=["true", "true"]
        )
        _seed_profile_and_contract(store, spec, ["f1.a1", "f1.a2"])
        ctx = _ctx(sandbox, store, router)

        out = await run_behavior_probes_impl(ctx, "t-behavior")
        assert [o["assertion_id"] for o in out["observations"]] == ["f1.a1", "f1.a2"]

    @pytest.mark.asyncio
    async def test_runner_ignores_non_behavior_assertions(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        # Contract mixes a unit_test assertion in; only the behavior_probe one
        # must produce an observation (1:1 with behavior assertions, not all).
        profile = ProjectProfile(
            project_type=ProjectType.CLI,
            crate_layout="single",
            crates=[Crate(name="cli", type="binary", targets=["cli"])],
            behavior_probe=BehaviorProbeSpec(
                strategy="cli_assert_cmd_probe", endpoints_to_probe=["true"]
            ),
        )
        store.save_project_profile(profile)
        contract = ValidationContract(
            mission_id=MISSION,
            features=[
                Feature(
                    feature_id="f1",
                    description="mixed",
                    assertions=[
                        Assertion(
                            id="f1.a1",
                            statement="unit covered",
                            verification_method=VerificationMethod.UNIT_TEST,
                            verification_target="tests/foo.rs::bar",
                        ),
                        Assertion(
                            id="f1.a2",
                            statement="behavior covered",
                            verification_method=VerificationMethod.BEHAVIOR_PROBE,
                            verification_target="behavior_probe::f1.a2",
                        ),
                    ],
                )
            ],
        )
        store.save_validation_contract(contract)
        ctx = _ctx(sandbox, store, router)

        out = await run_behavior_probes_impl(ctx, "t-behavior")
        assert [o["assertion_id"] for o in out["observations"]] == ["f1.a2"]
