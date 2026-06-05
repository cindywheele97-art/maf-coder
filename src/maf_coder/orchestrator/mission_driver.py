"""MissionDriver (AGENT_TOOLS_SPEC §14).

Top-level coroutine that orchestrates a full mission. Owns: scheduler, agents,
sandbox, lifecycle.

Mission shape: init → profile → planning → per-milestone scheduled execution →
finalize, driven under a concurrent `MissionSupervisor` heartbeat. The multi-day
ergonomics are wired (Phase E), not stubs: the budget guard (`_seed_budget` +
`make_budget_guard`), the status-report hook (`make_status_report_hook`), and
resume/rollback (`resume` / `rollback` + checkpoint store). `_milestone_loop`
re-invokes the Orchestrator once per milestone until `complete_mission` sets
`mission_state.mission_complete`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

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
from ..schemas import (
    Checkpoint,
    MissionState,
    NetworkPolicy,
    Permission,
    Role,
    Task,
    TaskBudget,
)
from .budget import default_budget_config, make_budget_guard
from .checkpoint_store import CheckpointStore
from .inbox import make_inbox_poll_hook
from .project_profiler import profile_project
from .push import NullPushAdapter, PushAdapter
from .scheduler import Scheduler
from .status_report import DEFAULT_STATUS_INTERVAL, make_status_report_hook
from .supervisor import MissionSupervisor

logger = logging.getLogger(__name__)

# Safety backstop on the per-milestone re-invocation loop. A real mission ends
# far sooner via the Orchestrator's complete_mission signal; this only bounds a
# pathological loop (the budget guard is the real cost ceiling).
_MAX_MILESTONES = 50


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
    # Full mission budget (USD) seeded into budget.yaml so the budget guard has an
    # explicit ceiling from tick 1. None → the guard's default (see budget.py).
    total_budget_usd: float | None = None
    supervisor_tick_sec: float = 60.0
    # Phase E E-comms: status-report cadence + out-of-band push adapter (default
    # Null = rely on the rendered status_<n>.md/.json on disk).
    status_report_interval: timedelta = DEFAULT_STATUS_INTERVAL
    push_adapter: PushAdapter = field(default_factory=NullPushAdapter)


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
        # Effective Coder provider for the 异-provider rule's dynamic half. Use
        # the explicit config value if given, else derive it from the Coder's
        # configured primary model. Defensive: a malformed/partial router config
        # falls back to None (dynamic half stays off; static forbidden_providers
        # on validators still protects) rather than breaking mission construction.
        self.coder_provider_in_use: str | None = config.coder_provider_in_use
        if self.coder_provider_in_use is None:
            try:
                self.coder_provider_in_use = self.router.provider_for_role(
                    Role.CODER_WORKER.value
                )
            except Exception:
                logger.warning(
                    "could not derive coder_provider_in_use from router config; "
                    "dynamic 异-provider half disabled (static rule still applies)"
                )

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

        await self._seed_budget()

        await self._profile_and_save()

        if self.config.dry_run:
            logger.info("dry_run=True — skipping planning/execution loop")
            await self._finalize(result="dry_run_complete")
            return

        # Drive the per-milestone loop under one long-lived supervisor. Each
        # milestone re-invokes the Orchestrator in a fresh scheduler; the
        # supervisor (budget/status/inbox hooks) spans the whole loop.
        await self._run_under_supervisor(
            self._milestone_loop, result_on_complete="complete"
        )

    async def _milestone_loop(self) -> str:
        """Per-milestone driver loop (re-invokes the Orchestrator each milestone).

        Each iteration: advance ``current_milestone`` → re-invoke the Orchestrator
        in a fresh scheduler (it reviews the prior milestone's verdicts, may
        checkpoint it, then either dispatches THIS milestone's work or declares the
        mission done) → drain the dispatched DAG → inspect the turn deterministically.

        Returns an honest mission result (Rule: fail loud — a stalled/errored loop
        must NOT report "complete"):
          - "complete"            — Orchestrator signalled mission_complete
          - "orchestrator_error"  — a turn errored / produced no result
          - "stalled"             — a turn dispatched no work and didn't complete
          - "milestone_cap_reached" — hit the _MAX_MILESTONES backstop
        """
        for n in range(_MAX_MILESTONES):
            # Use plan.md's milestone name (the first planned milestone from
            # tasks.yaml not yet completed). Falls back to a synthetic boundary
            # index only before the plan exists (the bootstrap/planning turn).
            milestone_id = self._next_planned_milestone() or f"m{n}"
            self._set_current_milestone(milestone_id)
            scheduler = self._build_scheduler()
            self._scheduler = scheduler
            await scheduler.add_task(self._orchestrator_bootstrap_task(milestone_id))
            await scheduler.run()

            # After a real run the orchestrate task is terminal. If it isn't
            # (e.g. an empty/stubbed scheduler that never executed it), do NOT
            # block on wait_for — treat it as a turn with no usable result.
            if scheduler.task_status("orchestrate") not in ("complete", "failed", "blocked"):
                logger.warning(
                    "milestone loop: orchestrate did not run to completion at %s "
                    "(status=%s); ending",
                    milestone_id,
                    scheduler.task_status("orchestrate"),
                )
                return "orchestrator_error"

            try:
                result = await scheduler.wait_for("orchestrate")
            except (KeyError, RuntimeError) as e:
                logger.warning(
                    "milestone loop: orchestrator turn %s produced no result (%r); ending",
                    milestone_id,
                    e,
                )
                return "orchestrator_error"
            if result.errored:
                logger.warning(
                    "milestone loop: orchestrator turn %s errored (%s); ending",
                    milestone_id,
                    result.error_reason,
                )
                return "orchestrator_error"

            if self.store.load_mission_state().mission_complete:
                logger.info("milestone loop: mission_complete signalled at %s", milestone_id)
                return "complete"
            if result.tools_invoked.count("dispatch_task") == 0:
                logger.info(
                    "milestone loop: no work dispatched at %s and mission not complete; "
                    "ending (nothing left to do or stalled)",
                    milestone_id,
                )
                return "stalled"
        logger.warning(
            "milestone loop: hit _MAX_MILESTONES cap (%d); ending", _MAX_MILESTONES
        )
        return "milestone_cap_reached"

    def _set_current_milestone(self, milestone_id: str) -> None:
        """Advance mission_state.current_milestone (immutable copy). No-op if the
        state file is missing or already at this milestone."""
        try:
            ms = self.store.load_mission_state()
        except FileNotFoundError:
            return
        if ms.current_milestone == milestone_id:
            return
        self.store.save_mission_state(ms.model_copy(update={"current_milestone": milestone_id}))

    def _planned_milestones(self) -> list[str]:
        """Ordered, distinct milestone ids from tasks.yaml's ``parent_milestone``
        fields — the canonical plan.md milestone names the Orchestrator authored.

        Empty (→ the loop falls back to a synthetic index) when tasks.yaml is
        absent or unparseable; never raises. tasks.yaml is a list of Task, written
        either at the top level or under a ``tasks:`` key — both are handled.
        """
        if not self.store.exists("tasks.yaml"):
            return []
        try:
            data: Any = yaml.safe_load(self.store.read_text("tasks.yaml"))
        except Exception:
            return []
        tasks = data.get("tasks") if isinstance(data, dict) else data
        if not isinstance(tasks, list):
            return []
        ordered: list[str] = []
        for t in tasks:
            if isinstance(t, dict):
                m = t.get("parent_milestone")
                if isinstance(m, str) and m and m not in ordered:
                    ordered.append(m)
        return ordered

    def _next_planned_milestone(self) -> str | None:
        """The first planned milestone (from tasks.yaml) not yet in
        ``completed_milestones``. None when there is no plan yet, or all planned
        milestones are complete. Keyed on completion state — not turn index — so it
        is correct despite the one-turn offset between dispatching a milestone and
        checkpointing it."""
        planned = self._planned_milestones()
        if not planned:
            return None
        try:
            completed = set(self.store.load_mission_state().completed_milestones)
        except FileNotFoundError:
            completed = set()
        for m in planned:
            if m not in completed:
                return m
        return None

    async def _run_under_supervisor(
        self, work: Callable[[], Awaitable[str | None]], *, result_on_complete: str
    ) -> None:
        """Run ``work`` under a concurrent supervisor heartbeat.

        Shared by ``start()`` (work = the milestone loop) and ``resume()`` (work =
        a single scheduler run). The supervisor is the Phase E spine: a heartbeat
        tick loop that later workstreams plug hooks into. It must be started before
        ``work`` and stopped on EVERY exit path (complete / aborted / crashed) — and
        a supervisor failure must NEVER change the mission result.
        """
        stop_event = asyncio.Event()
        supervisor = MissionSupervisor(
            store=self.store,
            event_log=self.event_log,
            mission_id=self.mission_id,
            started_at=self._started_at or datetime.now(UTC),
            tick_interval_sec=self.config.supervisor_tick_sec,
        )
        # --- Phase E supervision hooks (run on both start and resume paths) ---
        # E-guard (§E5): budget guard — bands at 50/80/100/150%, sets
        # mission_state.budget_mode, scheduler honors "paused".
        supervisor.register(make_budget_guard())
        # E-comms (§E2/E3): status-report timer + user-message inbox poll.
        supervisor.register(
            make_status_report_hook(
                interval=self.config.status_report_interval,
                push=self.config.push_adapter,
            )
        )
        supervisor.register(make_inbox_poll_hook())
        # --- end Phase E supervision hooks ---
        sup_task = asyncio.create_task(supervisor.run(stop_event))
        try:
            try:
                outcome = await work()
            except asyncio.CancelledError:
                logger.warning("MissionDriver: cancelled")
                await self._finalize(result="aborted")
                raise
            except Exception as e:
                logger.exception("MissionDriver crashed: %r", e)
                await self._finalize(result="crashed")
                raise

            # `work` may return an honest outcome (the milestone loop does); fall
            # back to result_on_complete when it returns None (the resume path).
            await self._finalize(result=outcome or result_on_complete)
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

            async def _run_resumed_scheduler() -> None:
                await scheduler.run()

            await self._run_under_supervisor(
                _run_resumed_scheduler, result_on_complete="resumed_complete"
            )
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
            coder_provider_in_use=self.coder_provider_in_use,
        )
        self.store.save_mission_state(ms)

    async def _seed_budget(self) -> None:
        """Write a default budget.yaml so the budget guard has an explicit,
        operator-editable ceiling from tick 1 — instead of relying on the
        single-turn Orchestrator to produce one. Idempotent: never overwrites an
        existing budget.yaml (operator-edited or carried over by a resumed run)."""
        if self.store.exists("budget.yaml"):
            return
        self.store.write_yaml(
            "budget.yaml", default_budget_config(self.config.total_budget_usd)
        )

    async def _profile_and_save(self) -> None:
        if self.store.exists("project_profile.yaml"):
            return
        try:
            profile = profile_project(self.config.repo_path)
            self.store.save_project_profile(profile)
        except Exception as e:
            logger.warning("MissionDriver: profile_project failed: %r", e)

    def _orchestrator_bootstrap_task(self, milestone_id: str = "m0") -> Task:
        """The Orchestrator turn for one milestone (re-invoked per milestone).

        The Orchestrator agent runs this, then plans + locks the validation
        contract (first turn only) + dispatches the worker/validator DAG via
        `dispatch_task`. Its own task is added directly (not via `dispatch_task`),
        so it is not gated by the contract that doesn't exist yet. `allowed_tools=[]`
        means "no per-task tool restriction" — the agent's tool registry scopes it.

        The Driver re-invokes this once per milestone; the turn either dispatches
        the current milestone's work, or — once the goal is delivered — calls
        `complete_mission` to end the loop.
        """
        # First turn == the planning turn: plan.md does not exist yet. (Keyed on
        # plan.md, not the milestone name, so a plan that names a milestone "m0"
        # is not mistaken for the bootstrap turn.)
        first = not self.store.exists("plan.md")
        background = (
            "Mission bootstrap. The ProjectProfile and mission_state exist. "
            "Produce plan.md, lock validation_contract.yaml, then dispatch the "
            f"first milestone's ({milestone_id}) worker/validator DAG with "
            "dispatch_task."
            if first
            else (
                f"Milestone boundary: now at {milestone_id}. Review the previous "
                "milestone's verdicts on disk and create_checkpoint it if it "
                "PASSED. Then either dispatch this milestone's worker/validator "
                "DAG with dispatch_task, OR — if the goal is fully delivered — "
                "call complete_mission to end the mission."
            )
        )
        return Task(
            task_id="orchestrate",
            parent_milestone=milestone_id,
            owner=Role.ORCHESTRATOR,
            goal=self.config.goal,
            background=background,
            acceptance_criteria=[],
            required_outputs=["plan.md", "validation_contract.yaml"],
            permission=Permission(
                allowed_paths=[],
                allowed_tools=[],
                network_policy=NetworkPolicy.NONE,
            ),
            budget=TaskBudget(max_tokens=200_000, max_runtime_sec=3600),
        )

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
            coder_provider_in_use=self.coder_provider_in_use,
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
