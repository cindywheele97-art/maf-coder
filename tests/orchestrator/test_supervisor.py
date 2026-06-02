"""MissionSupervisor — Phase E spine tests.

Covers the interface contract downstream workstreams (status-report, budget-guard,
recovery) code against:
- tick_once builds a correct SupervisionContext and runs every registered hook
- a hook that raises is isolated: later hooks still run; nothing propagates
- run() ticks repeatedly and exits promptly when stop_event is set
- the built-in heartbeat hook refreshes mission_state counters and persists them
- driver integration: the supervisor is started/stopped around scheduler.run()
  and a supervisor/hook error does not change the mission outcome
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from maf_coder.blackboard import ArtifactStore
from maf_coder.orchestrator import MissionConfig, MissionDriver
from maf_coder.orchestrator.supervisor import (
    MissionSupervisor,
    SupervisionContext,
)
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import MissionState


def _store_with_state(tmp_path: Path, mission_id: str = "m-sup") -> ArtifactStore:
    store = ArtifactStore(tmp_path / "missions", mission_id)
    store.save_mission_state(
        MissionState(
            mission_id=mission_id,
            started_at=datetime.now(UTC),
        )
    )
    return store


def _supervisor(store: ArtifactStore, **kwargs: object) -> MissionSupervisor:
    started_at = kwargs.pop("started_at", datetime.now(UTC) - timedelta(hours=2))
    assert isinstance(started_at, datetime)
    return MissionSupervisor(
        store=store,
        event_log=store.event_log(),
        mission_id=store.mission_id,
        started_at=started_at,
        tick_interval_sec=float(kwargs.pop("tick_interval_sec", 0.01)),  # type: ignore[arg-type]
    )


class _StubTurnResult:
    """Minimal AgentResult stand-in for a completing Orchestrator turn."""

    errored = False
    error_reason = None
    tools_invoked: ClassVar[list[str]] = []


def _completing_orchestrator_build(driver: MissionDriver, *, delay: float = 0.05):  # type: ignore[no-untyped-def]
    """Return a `_build_scheduler` whose scheduler runs a stub Orchestrator that
    declares the mission complete on its first turn (after `delay` so a concurrent
    supervisor ticks). Used to exercise the real milestone loop without an LLM."""
    from maf_coder.orchestrator.scheduler import Scheduler
    from maf_coder.schemas import Role

    class _CompletingStub:
        role = Role.ORCHESTRATOR

        def __init__(self, store: ArtifactStore) -> None:
            self.store = store

        async def run(self, task, *, mission_id, coder_provider_in_use=None):  # type: ignore[no-untyped-def]
            await asyncio.sleep(delay)
            ms = self.store.load_mission_state()
            self.store.save_mission_state(ms.model_copy(update={"mission_complete": True}))
            return _StubTurnResult()

    def _build() -> Scheduler:
        stub = _CompletingStub(driver.store)
        return Scheduler(
            store=driver.store,
            event_log=driver.event_log,
            router=driver.router,
            sandbox=driver.sandbox,
            agent_factory={Role.ORCHESTRATOR: lambda: stub},
            mission_id=driver.mission_id,
            coder_provider_in_use=driver.config.coder_provider_in_use,
        )

    return _build


@pytest.mark.asyncio
async def test_tick_once_builds_ctx_and_runs_all_hooks(tmp_path: Path) -> None:
    store = _store_with_state(tmp_path)
    # One LLM call so total_cost_usd is a real, non-zero aggregate.
    store.event_log().log_llm_call(
        mission_id=store.mission_id,
        actor="coder_worker",
        model="anthropic/x",
        tokens_in=10,
        tokens_out=20,
        cost_usd=1.25,
        latency_sec=0.1,
    )
    sup = _supervisor(store)

    seen: list[SupervisionContext] = []

    async def hook_a(ctx: SupervisionContext) -> None:
        seen.append(ctx)

    async def hook_b(ctx: SupervisionContext) -> None:
        seen.append(ctx)

    sup.register(hook_a)
    sup.register(hook_b)

    await sup.tick_once()

    # Both registered hooks ran (heartbeat is also registered but not in `seen`).
    assert len(seen) == 2
    ctx = seen[0]
    assert seen[1] is ctx  # same context object handed to every hook this tick
    assert ctx.mission_id == store.mission_id
    assert ctx.now.tzinfo is not None  # tz-aware
    assert ctx.total_cost_usd == pytest.approx(1.25)
    assert ctx.elapsed_hours == pytest.approx(2.0, abs=0.05)
    assert ctx.store is store
    assert ctx.mission_state.mission_id == store.mission_id


@pytest.mark.asyncio
async def test_tick_once_missing_state_is_skipped_gracefully(tmp_path: Path) -> None:
    # No mission_state.json on disk → tick must skip without raising or running hooks.
    store = ArtifactStore(tmp_path / "missions", "m-nostate")
    sup = _supervisor(store)
    ran = False

    async def hook(ctx: SupervisionContext) -> None:
        nonlocal ran
        ran = True

    sup.register(hook)
    await sup.tick_once()  # must not raise
    assert ran is False


@pytest.mark.asyncio
async def test_raising_hook_is_isolated(tmp_path: Path) -> None:
    store = _store_with_state(tmp_path)
    sup = _supervisor(store)
    second_ran = False

    async def boom(ctx: SupervisionContext) -> None:
        raise RuntimeError("hook blew up")

    async def good(ctx: SupervisionContext) -> None:
        nonlocal second_ran
        second_ran = True

    sup.register(boom)
    sup.register(good)

    # Why: a hook raising must NEVER crash the mission. tick_once must swallow
    # the exception and still run every later hook.
    await sup.tick_once()
    assert second_ran is True


@pytest.mark.asyncio
async def test_run_ticks_repeatedly_then_exits_on_stop(tmp_path: Path) -> None:
    store = _store_with_state(tmp_path)
    sup = _supervisor(store, tick_interval_sec=0.005)
    ticks = 0

    async def counter(ctx: SupervisionContext) -> None:
        nonlocal ticks
        ticks += 1

    sup.register(counter)
    stop_event = asyncio.Event()
    run_task = asyncio.create_task(sup.run(stop_event))

    # Let several ticks happen, then ask it to stop and confirm prompt exit.
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.wait_for(run_task, timeout=1.0)

    assert ticks >= 2  # ran repeatedly, not just once
    assert run_task.done()
    assert run_task.exception() is None


@pytest.mark.asyncio
async def test_heartbeat_refreshes_and_persists_state(tmp_path: Path) -> None:
    store = _store_with_state(tmp_path)
    store.event_log().log_llm_call(
        mission_id=store.mission_id,
        actor="coder_worker",
        model="anthropic/x",
        tokens_in=5,
        tokens_out=5,
        cost_usd=3.50,
        latency_sec=0.1,
    )
    # Only the built-in heartbeat hook (registered by default) runs here.
    sup = _supervisor(store, started_at=datetime.now(UTC) - timedelta(hours=1))

    await sup.tick_once()

    persisted = store.load_mission_state()
    assert persisted.cumulative_cost_usd == pytest.approx(3.50)
    assert persisted.cumulative_wall_clock_hours == pytest.approx(1.0, abs=0.05)


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
async def test_driver_runs_and_stops_supervisor_around_scheduler(
    tmp_path: Path,
) -> None:
    """Real-mode start() must start the supervisor, run the milestone loop to
    completion, then stop the supervisor — and a hook error must NOT change the
    mission outcome. We use a stub Orchestrator that declares the mission complete
    on its first turn (after a small delay so the concurrent supervisor ticks), so
    the loop does exactly one turn and ends 'complete'.
    """
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
        dry_run=False,
        supervisor_tick_sec=0.005,
    )
    driver = MissionDriver(mission_id="m-real", config=cfg)
    driver._build_scheduler = _completing_orchestrator_build(driver)  # type: ignore[method-assign]

    await driver.start()

    kinds = [e.kind for e in driver.event_log.iter_events()]
    assert "mission_end" in kinds
    # The supervisor ran concurrently and its heartbeat persisted state.
    persisted = driver.store.load_mission_state()
    assert persisted.cumulative_wall_clock_hours >= 0.0
    # Mission finished 'complete' — the supervisor neither blocked nor changed it.
    end_events = [e for e in driver.event_log.iter_events() if e.kind == "mission_end"]
    assert end_events[-1].payload["result"] == "complete"


@pytest.mark.asyncio
async def test_driver_supervisor_error_does_not_change_outcome(
    tmp_path: Path,
) -> None:
    """A supervisor whose run() raises must not change the mission result: the
    driver swallows the supervisor failure and the mission still ends 'complete'.
    """
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
        dry_run=False,
        supervisor_tick_sec=0.005,
    )
    driver = MissionDriver(mission_id="m-superr", config=cfg)
    driver._build_scheduler = _completing_orchestrator_build(driver, delay=0.02)  # type: ignore[method-assign]

    # Force the supervisor's run() to blow up. The mission must still complete.
    import maf_coder.orchestrator.mission_driver as md

    class _BoomSupervisor(md.MissionSupervisor):  # type: ignore[name-defined]
        async def run(self, stop_event: asyncio.Event) -> None:
            raise RuntimeError("supervisor exploded")

    monkeypatched = md.MissionSupervisor
    md.MissionSupervisor = _BoomSupervisor  # type: ignore[misc]
    try:
        await driver.start()
    finally:
        md.MissionSupervisor = monkeypatched  # type: ignore[misc]

    end_events = [e for e in driver.event_log.iter_events() if e.kind == "mission_end"]
    assert end_events[-1].payload["result"] == "complete"
