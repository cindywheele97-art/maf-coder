"""MissionDriver dry-run tests (AGENT_TOOLS_SPEC §14)."""

from __future__ import annotations

from pathlib import Path

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


class _StubOrchestrator:
    """Stands in for the real Orchestrator agent so the seed runs without an LLM."""

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
    assert ended[-1].payload.get("result") == "complete"
