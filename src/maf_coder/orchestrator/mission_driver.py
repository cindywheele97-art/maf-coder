"""MissionDriver (AGENT_TOOLS_SPEC §14).

Top-level coroutine that orchestrates a full mission. Owns: scheduler, agents,
sandbox, lifecycle.

Phase B scope: dry-run-capable orchestration with the minimum viable mission
shape (init → profile → planning → scheduled execution → finalize). Multi-day
ergonomics (status report timer, budget guard, resume) are stubs that exist
to satisfy the interface contract and will be filled in as Phase C work
proceeds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..agents.base import BaseAgent
from ..agents.behavior import BehaviorValidatorAgent
from ..agents.coder import CoderWorkerAgent
from ..agents.orchestrator import OrchestratorAgent
from ..agents.research import ResearchWorkerAgent
from ..agents.review import ReviewValidatorAgent
from ..agents.security import SecurityWorkerAgent
from ..blackboard import ArtifactStore
from ..models import ModelRouter
from ..sandbox import LocalShellSandbox, SandboxClient
from ..schemas import MissionState, Role
from .project_profiler import profile_project
from .scheduler import Scheduler

logger = logging.getLogger(__name__)


@dataclass
class MissionConfig:
    """Configuration for one mission."""

    missions_root: Path
    repo_path: Path
    router_config: Path
    goal: str
    sandbox_factory: Callable[[], SandboxClient] = field(
        default_factory=lambda: lambda: LocalShellSandbox()
    )
    dry_run: bool = False
    coder_provider_in_use: str | None = None


class MissionDriver:
    """Top-level mission orchestrator. Constructs everything and drives the loop."""

    def __init__(
        self,
        *,
        mission_id: str,
        config: MissionConfig,
    ) -> None:
        self.mission_id = mission_id
        self.config = config
        self.store = ArtifactStore(config.missions_root, mission_id)
        self.event_log = self.store.event_log()
        self.router = ModelRouter(config.router_config)
        self.sandbox = config.sandbox_factory()
        self._scheduler: Scheduler | None = None
        self._started_at: datetime | None = None

    # -- Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._started_at = datetime.now(UTC)
        self.event_log.log_mission_start(
            mission_id=self.mission_id,
            goal=self.config.goal,
            repo=str(self.config.repo_path),
            expected_budget_usd=None,
        )

        await self.sandbox.start(workspace_mount=self.config.repo_path)

        await self._initialize_state()

        await self._profile_and_save()

        if self.config.dry_run:
            logger.info("dry_run=True — skipping planning/execution loop")
            await self._finalize(result="dry_run_complete")
            return

        # Real-mode planning + execution would happen here. We provide the
        # wiring so future Phase B/C work can drop in without touching the
        # driver's structure.
        scheduler = self._build_scheduler()
        self._scheduler = scheduler
        try:
            await scheduler.run()
        except asyncio.CancelledError:
            logger.warning("MissionDriver: cancelled")
            await self._finalize(result="aborted")
            raise
        except Exception as e:
            logger.exception("MissionDriver crashed: %r", e)
            await self._finalize(result="crashed")
            raise

        await self._finalize(result="complete")

    async def stop(self, *, graceful: bool = True) -> None:
        try:
            await self.sandbox.stop(preserve_volumes=True)
        except Exception:
            logger.exception("MissionDriver.stop: sandbox.stop failed")
        if graceful:
            self.event_log.log_mission_end(
                mission_id=self.mission_id,
                result="stopped",
                total_cost_usd=self.event_log.total_cost_usd(),
                total_wall_clock_hours=self._elapsed_hours(),
            )

    async def resume(self, from_milestone: str | None = None) -> None:
        """Resume a previously-started mission. Phase C+ work; raises NotImplemented."""
        raise NotImplementedError("resume is not yet implemented (Phase C scope)")

    # -- Internals ---------------------------------------------------------

    async def _initialize_state(self) -> None:
        try:
            self.store.load_mission_state()
            return
        except FileNotFoundError:
            pass
        ms = MissionState(
            mission_id=self.mission_id,
            started_at=self._started_at or datetime.now(UTC),
            coder_provider_in_use=self.config.coder_provider_in_use,
        )
        self.store.save_mission_state(ms)

    async def _profile_and_save(self) -> None:
        if self.store.exists("project_profile.yaml"):
            return
        try:
            profile = profile_project(self.config.repo_path)
            self.store.save_project_profile(profile)
        except Exception as e:
            logger.warning("MissionDriver: profile_project failed: %r", e)

    def _build_scheduler(self) -> Scheduler:
        # Build agent factories. We bind real prompts; tests will skip start().
        common: dict[str, Any] = dict(
            store=self.store,
            event_log=self.event_log,
            router=self.router,
            sandbox=self.sandbox,
        )
        orch = OrchestratorAgent(**common)
        coder = CoderWorkerAgent(**common)
        reviewer = ReviewValidatorAgent(**common)
        researcher = ResearchWorkerAgent(**common)
        security = SecurityWorkerAgent(**common)
        behavior = BehaviorValidatorAgent(**common)

        agent_factory: dict[Role, Callable[[], BaseAgent[Any]]] = {
            Role.ORCHESTRATOR: lambda: orch,
            Role.CODER_WORKER: lambda: coder,
            Role.REVIEW_VALIDATOR: lambda: reviewer,
            Role.RESEARCH_WORKER: lambda: researcher,
            Role.SECURITY_WORKER: lambda: security,
            Role.BEHAVIOR_VALIDATOR: lambda: behavior,
        }
        scheduler = Scheduler(
            store=self.store,
            event_log=self.event_log,
            router=self.router,
            sandbox=self.sandbox,
            agent_factory=agent_factory,
            mission_id=self.mission_id,
            coder_provider_in_use=self.config.coder_provider_in_use,
        )
        orch.attach_scheduler(scheduler)
        return scheduler

    def _elapsed_hours(self) -> float:
        if self._started_at is None:
            return 0.0
        delta = datetime.now(UTC) - self._started_at
        return delta.total_seconds() / 3600.0

    async def _finalize(self, *, result: str) -> None:
        try:
            await self.sandbox.stop(preserve_volumes=True)
        except Exception:
            logger.exception("MissionDriver._finalize: sandbox.stop failed")
        self.event_log.log_mission_end(
            mission_id=self.mission_id,
            result=result,
            total_cost_usd=self.event_log.total_cost_usd(),
            total_wall_clock_hours=self._elapsed_hours(),
        )


__all__ = ["MissionConfig", "MissionDriver"]
