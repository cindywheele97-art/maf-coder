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
from ..schemas import Checkpoint, MissionState, Role
from .budget import make_budget_guard
from .checkpoint_store import CheckpointStore
from .project_profiler import profile_project
from .scheduler import Scheduler
from .supervisor import MissionSupervisor

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
    supervisor_tick_sec: float = 60.0


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
        await self._run_with_supervisor(scheduler, result_on_complete="complete")

    async def _run_with_supervisor(
        self, scheduler: Scheduler, *, result_on_complete: str
    ) -> None:
        """Run the scheduler under a concurrent supervisor heartbeat.

        Shared by ``start()`` and ``resume()``. The supervisor is the Phase E
        spine: a heartbeat tick loop that later workstreams plug hooks into. It
        must be started before scheduler.run() and stopped on EVERY exit path
        (complete / aborted / crashed) — and a supervisor failure must NEVER
        change the mission result.
        """
        stop_event = asyncio.Event()
        supervisor = MissionSupervisor(
            store=self.store,
            event_log=self.event_log,
            mission_id=self.mission_id,
            started_at=self._started_at or datetime.now(UTC),
            tick_interval_sec=self.config.supervisor_tick_sec,
        )
        # E-guard (Phase E §E5): budget guard hook — bands at 50/80/100/150%,
        # sets mission_state.budget_mode, scheduler honors "paused". Marked for
        # clean merge with E-comms (which adds its own register lines here).
        supervisor.register(make_budget_guard())
        sup_task = asyncio.create_task(supervisor.run(stop_event))
        try:
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

            await self._finalize(result=result_on_complete)
        finally:
            await self._stop_supervisor(stop_event, sup_task)

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
        """Resume a previously-started mission from a checkpoint.

        Loads mission_state, picks the target checkpoint (the named milestone,
        else the latest completed one), restores the sandbox from that
        checkpoint's snapshot, resets mission_state to that position, then
        re-enters the execution path for not-yet-complete work. A dry-run
        mission resumes end-to-end (restore + finalize) with no scheduler.

        FileNotFoundError-safe: a missing mission_state or checkpoint surfaces
        as a clear error rather than a crash. The sandbox is always stopped on
        every exit path.
        """
        self._started_at = datetime.now(UTC)
        try:
            state = self.store.load_mission_state()
        except FileNotFoundError as e:
            raise FileNotFoundError(
                f"resume: mission_state.json missing for {self.mission_id}"
            ) from e

        cp_store = CheckpointStore(self.store)
        checkpoint = cp_store.resolve_target(state, from_milestone)

        await self.sandbox.start(workspace_mount=self.config.repo_path)
        try:
            self.event_log.log_mission_start(
                mission_id=self.mission_id,
                goal=self.config.goal,
                repo=str(self.config.repo_path),
                expected_budget_usd=None,
            )
            await self._restore_from_checkpoint(state, checkpoint)

            if self.config.dry_run:
                logger.info("resume dry_run=True — restored state+sandbox, skipping execution")
                await self._finalize(result="resume_dry_run_complete")
                return

            scheduler = self._build_scheduler()
            self._scheduler = scheduler
            await self._run_with_supervisor(scheduler, result_on_complete="resumed_complete")
        finally:
            await self._ensure_sandbox_stopped()

    async def rollback(self, to_milestone: str) -> None:
        """Roll the mission back to an EARLIER checkpoint.

        Restores that checkpoint's snapshot, truncates
        ``completed_milestones`` to those at/before ``to_milestone``, sets
        ``current_milestone`` to it, and saves. Refuses to roll *forward*:
        ``to_milestone`` must already be a completed milestone.

        FileNotFoundError-safe and always stops the sandbox.
        """
        try:
            state = self.store.load_mission_state()
        except FileNotFoundError as e:
            raise FileNotFoundError(
                f"rollback: mission_state.json missing for {self.mission_id}"
            ) from e

        if to_milestone not in state.completed_milestones:
            raise ValueError(
                f"rollback: {to_milestone!r} is not a completed milestone; "
                f"completed={state.completed_milestones}. Cannot roll forward."
            )

        cp_store = CheckpointStore(self.store)
        checkpoint = cp_store.load(to_milestone)  # FileNotFoundError if absent

        await self.sandbox.start(workspace_mount=self.config.repo_path)
        try:
            await self._restore_snapshot(checkpoint)
            idx = state.completed_milestones.index(to_milestone)
            truncated = state.completed_milestones[: idx + 1]
            new_state = state.model_copy(
                update={
                    "completed_milestones": truncated,
                    "current_milestone": to_milestone,
                }
            )
            self.store.save_mission_state(new_state)
            self.event_log.log_checkpoint_created(
                mission_id=self.mission_id,
                milestone_id=to_milestone,
                git_tag=checkpoint.git_tag,
                snapshot_id=checkpoint.sandbox_snapshot_id,
            )
        finally:
            await self._ensure_sandbox_stopped()

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

    async def _stop_supervisor(
        self, stop_event: asyncio.Event, sup_task: asyncio.Task[None]
    ) -> None:
        """Always stop the supervisor and await its task. A supervisor failure
        must not change the mission result, so its exception is swallowed (and
        logged) here rather than propagated.
        """
        stop_event.set()
        try:
            await sup_task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MissionDriver: supervisor task failed (ignored)")

    async def _restore_snapshot(self, checkpoint: Checkpoint) -> None:
        """Restore the sandbox from a checkpoint's snapshot, if one exists.

        A snapshot id of '' / 'unknown' (commit_snapshot was best-effort and
        failed at checkpoint time) is treated as "nothing to restore" — the
        state reset still proceeds.
        """
        snap = checkpoint.sandbox_snapshot_id
        if not snap or snap == "unknown":
            logger.warning(
                "restore: checkpoint %s has no usable snapshot id; skipping sandbox restore",
                checkpoint.milestone_id,
            )
            return
        await self.sandbox.restore_snapshot(snap)

    async def _restore_from_checkpoint(
        self, state: MissionState, checkpoint: Checkpoint
    ) -> None:
        """Restore sandbox + reset mission_state to the checkpoint's position."""
        await self._restore_snapshot(checkpoint)
        milestone = checkpoint.milestone_id
        if milestone in state.completed_milestones:
            idx = state.completed_milestones.index(milestone)
            completed = state.completed_milestones[: idx + 1]
        else:
            completed = list(state.completed_milestones)
        new_state = state.model_copy(
            update={
                "completed_milestones": completed,
                "current_milestone": milestone,
            }
        )
        self.store.save_mission_state(new_state)

    async def _ensure_sandbox_stopped(self) -> None:
        """Stop the sandbox, swallowing+logging any error (finally-safe)."""
        try:
            await self.sandbox.stop(preserve_volumes=True)
        except Exception:
            logger.exception("MissionDriver: sandbox.stop failed (ignored)")

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
