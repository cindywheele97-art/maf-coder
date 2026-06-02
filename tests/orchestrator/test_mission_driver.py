"""MissionDriver dry-run tests (AGENT_TOOLS_SPEC §14)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from maf_coder.orchestrator import MissionConfig, MissionDriver
from maf_coder.orchestrator.scheduler import Scheduler
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import Role


def _write_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n[[bin]]\nname = "demo"\n',
        encoding="utf-8",
    )


def _write_router(path: Path) -> None:
    path.write_text(
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
        "    fallback: []\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_dry_run_produces_profile_and_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_repo(repo)
    router_cfg = tmp_path / "droid.yaml"
    _write_router(router_cfg)
    cfg = MissionConfig(
        missions_root=tmp_path / "missions",
        repo_path=repo,
        router_config=router_cfg,
        goal="demo",
        sandbox_factory=lambda: LocalShellSandbox(),
        dry_run=True,
    )
    driver = MissionDriver(mission_id="m-dry", config=cfg)
    await driver.start()
    assert (cfg.missions_root / "m-dry" / "project_profile.yaml").exists()
    assert (cfg.missions_root / "m-dry" / "mission_state.json").exists()
    events = list(driver.event_log.iter_events())
    kinds = [e.kind for e in events]
    assert "mission_start" in kinds
    assert "mission_end" in kinds


@pytest.mark.asyncio
async def test_start_seeds_budget_yaml(tmp_path: Path) -> None:
    """A new mission must have budget.yaml on disk so the budget guard engages
    from tick 1 — not left to the (single-turn) Orchestrator to produce."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    router_cfg = tmp_path / "droid.yaml"
    _write_router(router_cfg)
    cfg = MissionConfig(
        missions_root=tmp_path / "missions",
        repo_path=repo,
        router_config=router_cfg,
        goal="demo",
        dry_run=True,
    )
    driver = MissionDriver(mission_id="m-bud", config=cfg)
    await driver.start()
    budget_path = cfg.missions_root / "m-bud" / "budget.yaml"
    assert budget_path.exists()
    cfg_dict = driver.store.read_yaml("budget.yaml")
    assert cfg_dict == {"total_budget_usd": 100.0, "alert_threshold_usd": 50.0}


@pytest.mark.asyncio
async def test_start_honors_explicit_budget(tmp_path: Path) -> None:
    """--budget-usd (MissionConfig.total_budget_usd) sets the ceiling the guard
    reads back, so the operator can size a long run at launch."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    router_cfg = tmp_path / "droid.yaml"
    _write_router(router_cfg)
    cfg = MissionConfig(
        missions_root=tmp_path / "missions",
        repo_path=repo,
        router_config=router_cfg,
        goal="demo",
        dry_run=True,
        total_budget_usd=500.0,
    )
    driver = MissionDriver(mission_id="m-bud2", config=cfg)
    await driver.start()
    assert driver.store.read_yaml("budget.yaml") == {
        "total_budget_usd": 500.0,
        "alert_threshold_usd": 250.0,
    }


@pytest.mark.asyncio
async def test_start_does_not_overwrite_existing_budget(tmp_path: Path) -> None:
    """Idempotent: an operator-edited (or resumed) budget.yaml survives start()."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    router_cfg = tmp_path / "droid.yaml"
    _write_router(router_cfg)
    cfg = MissionConfig(
        missions_root=tmp_path / "missions",
        repo_path=repo,
        router_config=router_cfg,
        goal="demo",
        dry_run=True,
    )
    driver = MissionDriver(mission_id="m-bud3", config=cfg)
    # Operator wrote a custom budget before launch.
    driver.store.write_yaml("budget.yaml", {"total_budget_usd": 42.0})
    await driver.start()
    assert driver.store.read_yaml("budget.yaml") == {"total_budget_usd": 42.0}


def test_coder_provider_derived_from_router(tmp_path: Path) -> None:
    """When config leaves coder_provider_in_use=None, the driver derives it from
    the router's coder_worker primary (so the dynamic 异-provider half engages)."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    router_cfg = tmp_path / "droid.yaml"
    _write_router(router_cfg)  # coder_worker -> anthropic/x
    cfg = MissionConfig(
        missions_root=tmp_path / "missions",
        repo_path=repo,
        router_config=router_cfg,
        goal="demo",
        dry_run=True,
    )
    driver = MissionDriver(mission_id="m-cp", config=cfg)
    assert driver.coder_provider_in_use == "anthropic"


def test_coder_provider_explicit_override_wins(tmp_path: Path) -> None:
    """An explicit config value is used verbatim — no derivation."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    router_cfg = tmp_path / "droid.yaml"
    _write_router(router_cfg)
    cfg = MissionConfig(
        missions_root=tmp_path / "missions",
        repo_path=repo,
        router_config=router_cfg,
        goal="demo",
        dry_run=True,
        coder_provider_in_use="openai",
    )
    driver = MissionDriver(mission_id="m-cp2", config=cfg)
    assert driver.coder_provider_in_use == "openai"


def test_orchestrator_bootstrap_task_shape(tmp_path: Path) -> None:
    """The seed task is a valid ORCHESTRATOR task carrying the mission goal."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    router_cfg = tmp_path / "droid.yaml"
    _write_router(router_cfg)
    cfg = MissionConfig(
        missions_root=tmp_path / "missions",
        repo_path=repo,
        router_config=router_cfg,
        goal="add a /version endpoint",
        dry_run=True,
    )
    driver = MissionDriver(mission_id="m-seed", config=cfg)

    task = driver._orchestrator_bootstrap_task()
    assert task.task_id == "orchestrate"
    assert task.owner == Role.ORCHESTRATOR.value  # use_enum_values=True
    assert task.goal == "add a /version endpoint"
    assert task.depends_on == []
    assert task.permission.allowed_tools == []  # unrestricted


class _StubResult:
    errored = False
    error_reason = None
    tools_invoked: ClassVar[list[str]] = []


class _StubOrchestrator:
    """Stands in for the real Orchestrator agent so the seed runs without an LLM.

    Dispatches no work, so the milestone loop ends after one turn (m0)."""

    role = Role.ORCHESTRATOR

    def __init__(self) -> None:
        self.ran_with: str | None = None

    async def run(self, task, *, mission_id: str, coder_provider_in_use=None):  # type: ignore[no-untyped-def]
        self.ran_with = task.task_id
        return _StubResult()


class _SeedTestDriver(MissionDriver):
    """MissionDriver whose scheduler runs a STUB orchestrator (no real agents)."""

    def _build_scheduler(self) -> Scheduler:
        stub = _StubOrchestrator()
        return Scheduler(
            store=self.store,
            event_log=self.event_log,
            router=self.router,
            sandbox=self.sandbox,
            agent_factory={Role.ORCHESTRATOR: lambda: stub},
            mission_id=self.mission_id,
            coder_provider_in_use=self.config.coder_provider_in_use,
        )


@pytest.mark.asyncio
async def test_real_mode_seeds_and_runs_orchestrator(tmp_path: Path) -> None:
    """Real mode (dry_run=False) seeds the orchestrate task AND the scheduler
    actually runs it — proving the bootstrap loop is wired, not a no-op."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    router_cfg = tmp_path / "droid.yaml"
    _write_router(router_cfg)
    cfg = MissionConfig(
        missions_root=tmp_path / "missions",
        repo_path=repo,
        router_config=router_cfg,
        goal="add a /version endpoint",
        sandbox_factory=lambda: LocalShellSandbox(),
        dry_run=False,
        supervisor_tick_sec=0.05,
    )
    driver = _SeedTestDriver(mission_id="m-real", config=cfg)
    await driver.start()

    events = list(driver.event_log.iter_events())
    dispatched = [e for e in events if e.kind == "task_dispatched" and e.task_id == "orchestrate"]
    completed = [e for e in events if e.kind == "task_complete" and e.task_id == "orchestrate"]
    ended = [e for e in events if e.kind == "mission_end"]
    assert dispatched, "orchestrate task was never added to the DAG"
    assert completed, "orchestrate task was never run by the scheduler"
    assert ended, "mission never ended"
    # The stub dispatches no work and never signals complete → honest "stalled"
    # result (the loop must not report "complete" for a no-op turn).
    assert ended[-1].payload.get("result") == "stalled"


# --- Milestone re-invocation loop ------------------------------------------


class _LoopResult:
    """Minimal stand-in for AgentResult the milestone loop inspects."""

    errored = False
    error_reason = None

    def __init__(self, *, tools_invoked: list[str], errored: bool = False) -> None:
        self.tools_invoked = tools_invoked
        self.errored = errored


class _LoopStub:
    """Shared Orchestrator stub driving the loop via on-disk mission_state.

    Reads ``current_milestone`` (set by the Driver before each turn) to decide:
    dispatch work, declare the mission complete, dispatch nothing, or error.
    """

    role = Role.ORCHESTRATOR

    def __init__(  # type: ignore[no-untyped-def]
        self,
        store,
        *,
        complete_at: str | None,
        error_at: str | None = None,
        noop_at: str | None = None,
    ) -> None:
        self.store = store
        self.complete_at = complete_at
        self.error_at = error_at
        self.noop_at = noop_at
        self.turns: list[str | None] = []

    async def run(self, task, *, mission_id: str, coder_provider_in_use=None):  # type: ignore[no-untyped-def]
        cm = self.store.load_mission_state().current_milestone
        self.turns.append(cm)
        if cm == self.error_at:
            return _LoopResult(tools_invoked=[], errored=True)
        if cm == self.noop_at:
            return _LoopResult(tools_invoked=[])  # dispatched nothing, did NOT complete
        if cm == self.complete_at:
            ms = self.store.load_mission_state()
            self.store.save_mission_state(ms.model_copy(update={"mission_complete": True}))
            return _LoopResult(tools_invoked=[])  # declared done, dispatched nothing
        return _LoopResult(tools_invoked=["dispatch_task"])  # dispatched this milestone


class _LoopTestDriver(MissionDriver):
    """Driver whose every per-milestone scheduler shares one Orchestrator stub."""

    def set_stub(self, stub: _LoopStub) -> None:
        self._stub = stub

    def _build_scheduler(self) -> Scheduler:
        return Scheduler(
            store=self.store,
            event_log=self.event_log,
            router=self.router,
            sandbox=self.sandbox,
            agent_factory={Role.ORCHESTRATOR: lambda: self._stub},
            mission_id=self.mission_id,
            coder_provider_in_use=self.config.coder_provider_in_use,
        )


def _loop_cfg(tmp_path: Path) -> MissionConfig:
    repo = tmp_path / "repo"
    _write_repo(repo)
    router_cfg = tmp_path / "droid.yaml"
    _write_router(router_cfg)
    return MissionConfig(
        missions_root=tmp_path / "missions",
        repo_path=repo,
        router_config=router_cfg,
        goal="demo",
        sandbox_factory=lambda: LocalShellSandbox(),
        dry_run=False,
        supervisor_tick_sec=0.05,
    )


@pytest.mark.asyncio
async def test_milestone_loop_runs_until_complete(tmp_path: Path) -> None:
    """The Driver re-invokes the Orchestrator each milestone until it signals
    complete — m0 dispatch, m1 dispatch, m2 declares done = 3 turns."""
    driver = _LoopTestDriver(mission_id="m-loop", config=_loop_cfg(tmp_path))
    driver.set_stub(_LoopStub(driver.store, complete_at="m2"))
    await driver.start()

    assert driver._stub.turns == ["m0", "m1", "m2"]
    ms = driver.store.load_mission_state()
    assert ms.mission_complete is True
    assert ms.current_milestone == "m2"
    ended = [e for e in driver.event_log.iter_events() if e.kind == "mission_end"]
    assert ended[-1].payload.get("result") == "complete"


@pytest.mark.asyncio
async def test_milestone_loop_stops_when_no_work_dispatched(tmp_path: Path) -> None:
    """A turn that dispatches nothing and does not signal complete ends the loop
    (nothing left to do / stalled) — here on the very first turn."""
    driver = _LoopTestDriver(mission_id="m-noop", config=_loop_cfg(tmp_path))
    driver.set_stub(_LoopStub(driver.store, complete_at=None, noop_at="m0"))
    await driver.start()
    assert driver._stub.turns == ["m0"]  # stopped after one no-work turn
    ms = driver.store.load_mission_state()
    assert ms.mission_complete is False
    assert ms.current_milestone == "m0"  # never advanced past m0
    ended = [e for e in driver.event_log.iter_events() if e.kind == "mission_end"]
    assert ended[-1].payload.get("result") == "stalled"


@pytest.mark.asyncio
async def test_milestone_loop_stops_on_errored_turn(tmp_path: Path) -> None:
    """An errored Orchestrator turn ends the loop rather than re-invoking forever.

    The scheduler may retry the failed task within the milestone, so assert on
    milestone advancement (never reached m1), not raw invocation count."""
    driver = _LoopTestDriver(mission_id="m-err", config=_loop_cfg(tmp_path))
    driver.set_stub(_LoopStub(driver.store, complete_at=None, error_at="m0"))
    await driver.start()
    assert "m1" not in driver._stub.turns  # loop did not advance to the next milestone
    assert driver.store.load_mission_state().current_milestone == "m0"
    ended = [e for e in driver.event_log.iter_events() if e.kind == "mission_end"]
    assert ended[-1].payload.get("result") == "orchestrator_error"


@pytest.mark.asyncio
async def test_milestone_loop_respects_max_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stub that always dispatches and never completes is bounded by the
    _MAX_MILESTONES backstop rather than looping forever."""
    from maf_coder.orchestrator import mission_driver as md

    monkeypatch.setattr(md, "_MAX_MILESTONES", 3)
    driver = _LoopTestDriver(mission_id="m-cap", config=_loop_cfg(tmp_path))
    # complete_at=None never matches a milestone id, so every turn dispatches.
    driver.set_stub(_LoopStub(driver.store, complete_at=None))
    await driver.start()
    assert driver._stub.turns == ["m0", "m1", "m2"]  # capped at 3 turns
    ended = [e for e in driver.event_log.iter_events() if e.kind == "mission_end"]
    assert ended[-1].payload.get("result") == "milestone_cap_reached"
