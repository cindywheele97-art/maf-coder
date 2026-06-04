"""Scheduler — DAG executor with slot management and retries (AGENT_TOOLS_SPEC §13).

The Scheduler is intentionally minimal for Phase B:

- Tracks a set of `Task`s with `depends_on` edges.
- Holds at most one active worker per role for the "serialized" roles
  (CODER_WORKER, BEHAVIOR_VALIDATOR per soul.md §3.1). Other roles run
  in parallel up to a soft cap.
- Honors `Task.failure_handling.retry_budget` on transient failures.
- Logs TASK_DISPATCHED / TASK_COMPLETE / TASK_FAILED events.

Out of Phase B scope (Phase C+): work-stealing, cancellation propagation,
priority queues. The current implementation iterates over the DAG and picks
the next ready task on each tick.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..agents.base import AgentResult, BaseAgent
from ..agents.results import TaskHandle
from ..blackboard import ArtifactStore
from ..blackboard.event_log import Event, EventKind, EventLog
from ..models import ModelRouter, provider_of
from ..sandbox import SandboxClient
from ..schemas import Role, Task, VerdictResult
from ..validators.arbitration import (
    IMPLEMENTATION_PATH_ISSUE_SIGNAL,
    REPLAN_RISK_LEVEL,
    ArbitrationDecision,
    check_validator_preconditions,
)

logger = logging.getLogger(__name__)

TaskState = Literal["pending", "ready", "active", "complete", "failed", "blocked"]

_PARALLEL_LIMIT = {
    Role.CODER_WORKER: 1,
    Role.BEHAVIOR_VALIDATOR: 1,
}
_PARALLEL_DEFAULT = 4

# Budget modes (mirror MissionState.budget_mode / budget.py).
_MODE_PAUSED = "paused"
_MODE_COST_CONSCIOUS = "cost_conscious"

# Cost-conscious enforcement (soul.md §5.5): serialize parallel roles and cap
# per-task retries to curb spend once the budget guard crosses the 80% band.
_COST_CONSCIOUS_PARALLEL_CAP = 1
_COST_CONSCIOUS_RETRY_CAP = 1


@dataclass
class _TaskRecord:
    task: Task
    state: TaskState = "pending"
    attempts: int = 0
    result: AgentResult[Any] | None = None
    error: str | None = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)


class Scheduler:
    """DAG executor — owns the task table + role slot counts."""

    def __init__(
        self,
        *,
        store: ArtifactStore,
        event_log: EventLog,
        router: ModelRouter,
        sandbox: SandboxClient,
        agent_factory: dict[Role, Callable[[], BaseAgent[Any]]],
        mission_id: str,
        coder_provider_in_use: str | None = None,
    ) -> None:
        self.store = store
        self.event_log = event_log
        self.router = router
        self.sandbox = sandbox
        self.agent_factory = agent_factory
        self.mission_id = mission_id
        self.coder_provider_in_use = coder_provider_in_use
        self._tasks: dict[str, _TaskRecord] = {}
        self._active_by_role: dict[Role, int] = {r: 0 for r in Role}
        self._lock = asyncio.Lock()

    # -- DAG mutation ------------------------------------------------------

    def has_task(self, task_id: str) -> bool:
        return task_id in self._tasks

    def task_owner(self, task_id: str) -> str | None:
        """Return the owner role value of a known task, or None if unknown.

        Used by the dual-validator chain gate to resolve the review_validator
        dependency of a behavior_validator task without hardcoding task IDs.
        """
        rec = self._tasks.get(task_id)
        if rec is None:
            return None
        owner = rec.task.owner
        return owner.value if hasattr(owner, "value") else str(owner)

    async def add_task(self, task: Task) -> TaskHandle:
        if task.task_id in self._tasks:
            from ..agents.errors import TaskAlreadyDispatchedError

            raise TaskAlreadyDispatchedError(f"add_task: {task.task_id!r} already in DAG")
        self._tasks[task.task_id] = _TaskRecord(task=task)
        self.event_log.log_task_dispatched(
            mission_id=self.mission_id,
            task_id=task.task_id,
            owner=task.owner.value if hasattr(task.owner, "value") else str(task.owner),
            priority=task.priority.value if hasattr(task.priority, "value") else str(task.priority),
        )
        return TaskHandle(task_id=task.task_id, dispatched_at=time.monotonic())

    # -- Query -------------------------------------------------------------

    def task_status(self, task_id: str) -> TaskState:
        rec = self._tasks.get(task_id)
        return rec.state if rec else "pending"

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {
            s: 0 for s in ("pending", "ready", "active", "complete", "failed", "blocked")
        }
        for rec in self._tasks.values():
            counts[rec.state] += 1
        return {
            "total": len(self._tasks),
            **counts,
            "active_by_role": {r.value: c for r, c in self._active_by_role.items() if c},
        }

    # -- Readiness logic ---------------------------------------------------

    def _is_ready(self, rec: _TaskRecord, cost_conscious: bool = False) -> bool:
        if rec.state != "pending":
            return False
        # All dependencies complete
        for dep_id in rec.task.depends_on:
            dep = self._tasks.get(dep_id)
            if dep is None or dep.state != "complete":
                return False
        # Slot available for this role. Cost-conscious mode (soul.md §5.5)
        # serializes every role (cap 1) to close down parallel spend.
        owner = self._coerce_role(rec.task.owner)
        cap = _PARALLEL_LIMIT.get(owner, _PARALLEL_DEFAULT)
        if cost_conscious:
            cap = min(cap, _COST_CONSCIOUS_PARALLEL_CAP)
        return self._active_by_role[owner] < cap

    @staticmethod
    def _coerce_role(role: Any) -> Role:
        if isinstance(role, Role):
            return role
        return Role(role)

    # -- Dual-validator chain (Phase D §D3) --------------------------------

    def _review_dependency_id(self, rec: _TaskRecord) -> str | None:
        """Resolve the review_validator dependency of a behavior task by role.

        Generic — never matches literal IDs. Returns the first dependency owned
        by review_validator, or None if the behavior task has no such dependency.
        """
        for dep_id in rec.task.depends_on:
            dep = self._tasks.get(dep_id)
            if dep is None:
                continue
            if self._coerce_role(dep.task.owner) is Role.REVIEW_VALIDATOR:
                return dep_id
        return None

    def _behavior_chain_ok(self, rec: _TaskRecord) -> bool:
        """True iff this behavior task may run: it has a review_validator
        dependency whose verdict file exists and is PASS.
        """
        review_id = self._review_dependency_id(rec)
        if review_id is None:
            return False
        try:
            verdict = self.store.load_review_verdict(review_id)
        except FileNotFoundError:
            return False
        return verdict.result == VerdictResult.PASS.value

    def _block_behavior_task(self, rec: _TaskRecord) -> None:
        """Refuse a behavior task whose review gate is unsatisfied: mark blocked,
        emit an event carrying the implementation_path_issue signal, finish it.
        """
        review_id = self._review_dependency_id(rec)
        rec.state = "blocked"
        rec.error = "blocked: dual-validator chain — review verdict not PASS"
        self.event_log.append(
            Event(
                kind=EventKind.VALIDATOR_CHAIN_BLOCKED.value,
                mission_id=self.mission_id,
                trace_id=self.mission_id,
                task_id=rec.task.task_id,
                actor=Role.BEHAVIOR_VALIDATOR.value,
                payload={
                    "reason": rec.error,
                    "review_task_id": review_id,
                    "signal": "implementation_path_issue",
                },
            )
        )
        rec.done_event.set()

    # -- Budget pause gate (Phase E §E5) -----------------------------------

    def _budget_mode(self) -> str:
        """Current budget mode from mission_state, read fresh each scheduling pass
        so a transition set by the concurrent budget SupervisionHook takes effect
        on the next pass. Missing/unreadable state fails open to "normal" — a read
        error must never wedge or throttle a mission that isn't over budget.
        """
        try:
            return self.store.load_mission_state().budget_mode
        except Exception:
            return "normal"

    def _budget_paused(self) -> bool:
        """True iff the budget guard has driven the mission into "paused"."""
        return self._budget_mode() == _MODE_PAUSED

    def _block_paused_task(self, rec: _TaskRecord) -> None:
        """Refuse to launch a NEW task while budget_mode == "paused".

        Mirrors D3's `_block_behavior_task`: mark blocked, emit a single event
        carrying the budget signal, finish the task. Already-active tasks are
        untouched — only NEW dispatch is gated, so active work drains normally.
        """
        rec.state = "blocked"
        rec.error = "blocked: budget paused — no new tasks dispatched"
        self.event_log.append(
            Event(
                kind=EventKind.BUDGET_MODE_CHANGED.value,
                mission_id=self.mission_id,
                trace_id=self.mission_id,
                task_id=rec.task.task_id,
                actor="orchestrator",
                payload={
                    "reason": rec.error,
                    "to_mode": "paused",
                    "gated_dispatch": True,
                },
            )
        )
        rec.done_event.set()

    # -- 异-provider reconciliation (F2 / soul.md §3.5) --------------------

    def _reconcile_coder_provider(self, result: AgentResult[Any]) -> None:
        """Sync ``coder_provider_in_use`` to the provider the Coder actually ran on.

        ``coder_provider_in_use`` is derived once from the Coder's config primary,
        but the smart router (or a config change) can land the Coder on a different
        provider. If so, the validators' "≠ Coder provider" check would target the
        wrong provider and a validator could run on the SAME provider as the Coder —
        silently weakening the validator-independence invariant (soul.md §3.5).

        Updating the in-memory anchor fixes validators dispatched later this
        milestone (they depend on the Coder, so none have run yet); persisting to
        mission_state makes it durable + visible (``mission status``). No-op when
        the provider already matches.
        """
        model_used = getattr(result, "model_used", "") or ""
        if not model_used:
            return
        actual = provider_of(model_used)
        if actual == self.coder_provider_in_use:
            return
        logger.warning(
            "Coder ran on provider %r (model %s), not the assumed %r; reconciling "
            "coder_provider_in_use so validators enforce异-provider against the real "
            "provider (soul.md §3.5).",
            actual,
            model_used,
            self.coder_provider_in_use,
        )
        self.coder_provider_in_use = actual
        try:
            ms = self.store.load_mission_state()
            if ms.coder_provider_in_use != actual:
                self.store.save_mission_state(
                    ms.model_copy(update={"coder_provider_in_use": actual})
                )
        except Exception:
            logger.exception("Scheduler: failed to persist reconciled coder_provider_in_use")

    # -- Conflict arbitration (Phase D §D4) --------------------------------

    def _arbitrate_completed_behavior(self, rec: _TaskRecord) -> None:
        """After a behavior_validator task completes, reconcile its verdict with
        the review verdict and record the arbitration decision (§D4 table).

        Runs only on the path where the behavior task ACTUALLY executed (review
        PASS gate satisfied → behavior produced a verdict). The blocked path
        (review FAIL) is handled by `_block_behavior_task` in the run loop and is
        not re-arbitrated here.

        Reuses D3's `_review_dependency_id` to find the review task by owner role
        — never by literal ID. Side effects (event / escalation) live here; the
        decision itself is the pure `check_validator_preconditions`.
        """
        review_id = self._review_dependency_id(rec)
        if review_id is None:
            return
        decision = check_validator_preconditions(
            self.store,
            review_task_id=review_id,
            behavior_task_id=rec.task.task_id,
        )

        if decision is ArbitrationDecision.REPLAN_IMPLEMENTATION_PATH:
            # PASS + FAIL → orchestrator re-plans; carry the stuck-recovery signal
            # + risk=medium so the re-plan loop keys off a single token.
            self.event_log.log_validator_arbitration(
                mission_id=self.mission_id,
                behavior_task_id=rec.task.task_id,
                review_task_id=review_id,
                decision=decision.value,
                signal=IMPLEMENTATION_PATH_ISSUE_SIGNAL,
                risk_level=REPLAN_RISK_LEVEL,
            )
        elif decision is ArbitrationDecision.HUMAN_GATE:
            # FAIL + PASS → near-impossible contradiction; force-escalate. Reuse
            # the existing human-gate escalation event (log_escalation), then
            # record the arbitration decision for the audit trail.
            self.event_log.log_escalation(
                mission_id=self.mission_id,
                target="human_gate",
                reason=(
                    "validator conflict: review FAIL but behavior PASS "
                    f"(review_task_id={review_id})"
                ),
                task_id=rec.task.task_id,
            )
            self.event_log.log_validator_arbitration(
                mission_id=self.mission_id,
                behavior_task_id=rec.task.task_id,
                review_task_id=review_id,
                decision=decision.value,
            )
        elif decision is ArbitrationDecision.CHECKPOINT_CANDIDATE:
            # PASS + PASS → lightweight checkpoint-candidate signal. Phase E builds
            # real checkpointing; here we only flag the candidate via an event.
            self.event_log.log_validator_arbitration(
                mission_id=self.mission_id,
                behavior_task_id=rec.task.task_id,
                review_task_id=review_id,
                decision=decision.value,
            )
        # BEHAVIOR_BLOCKED: behavior never ran on this path; nothing to do.

    # -- Wait helpers ------------------------------------------------------

    async def wait_for(self, task_id: str, timeout_sec: float | None = None) -> AgentResult[Any]:
        rec = self._tasks.get(task_id)
        if rec is None:
            raise KeyError(f"wait_for: unknown task {task_id!r}")
        if timeout_sec is None:
            await rec.done_event.wait()
        else:
            await asyncio.wait_for(rec.done_event.wait(), timeout=timeout_sec)
        if rec.result is None:
            raise RuntimeError(
                f"wait_for: task {task_id!r} ended without a result "
                f"(state={rec.state}, error={rec.error})"
            )
        return rec.result

    async def cancel(self, task_id: str) -> None:
        rec = self._tasks.get(task_id)
        if rec is None or rec.state in ("complete", "failed"):
            return
        rec.state = "failed"
        rec.error = "cancelled"
        rec.done_event.set()

    # -- Run loop ----------------------------------------------------------

    async def run(self) -> None:
        """Main scheduler loop. Returns when every task is complete or failed."""
        active_tasks: set[asyncio.Task[Any]] = set()

        while True:
            launched_any = False
            # Phase E §E5 — read the budget mode once per scheduling pass. When
            # paused, NEW tasks are refused (blocked + event); in-flight tasks
            # drain normally. When cost_conscious (soul.md §5.5), every role is
            # serialized (cap 1) and retries are capped in `_run_one`.
            mode = self._budget_mode()
            paused = mode == _MODE_PAUSED
            cost_conscious = mode == _MODE_COST_CONSCIOUS
            async with self._lock:
                for rec in self._tasks.values():
                    if not self._is_ready(rec, cost_conscious):
                        continue
                    # Budget pause gate (Phase E §E5): refuse NEW dispatch while
                    # paused. Mirrors the D3 chain-gate block style.
                    if paused:
                        self._block_paused_task(rec)
                        continue
                    # Dual-validator chain — runtime verdict gate (Phase D §D3,
                    # condition (b)). A behavior_validator task may only run once
                    # its review_validator dependency has a PASS verdict on disk.
                    # Deps are already complete here (guaranteed by _is_ready), so
                    # the review verdict file exists if review actually passed.
                    if (
                        self._coerce_role(rec.task.owner) is Role.BEHAVIOR_VALIDATOR
                        and not self._behavior_chain_ok(rec)
                    ):
                        self._block_behavior_task(rec)
                        continue
                    rec.state = "active"
                    owner = self._coerce_role(rec.task.owner)
                    self._active_by_role[owner] += 1
                    t = asyncio.create_task(self._run_one(rec, cost_conscious))
                    active_tasks.add(t)
                    launched_any = True

            if not launched_any and not active_tasks:
                # Nothing scheduled and nothing in flight. Are we done?
                unfinished = [
                    r for r in self._tasks.values() if r.state not in ("complete", "failed")
                ]
                if not unfinished:
                    return
                # Otherwise: blocked DAG (e.g. dep of a failed task). Mark blocked
                # tasks then exit.
                for r in unfinished:
                    r.state = "blocked"
                    r.error = "blocked: dependency unmet"
                    r.done_event.set()
                return

            if active_tasks:
                done, pending = await asyncio.wait(
                    active_tasks, return_when=asyncio.FIRST_COMPLETED
                )
                active_tasks = pending
                for d in done:
                    exc = d.exception()
                    if exc is not None:
                        logger.exception("Scheduler: worker task crashed: %r", exc)
            else:
                # Avoid spinning when no slot was free even though tasks are pending.
                await asyncio.sleep(0.05)

    async def _run_one(self, rec: _TaskRecord, cost_conscious: bool = False) -> None:
        owner = self._coerce_role(rec.task.owner)
        try:
            agent = self.agent_factory[owner]()
        except KeyError:
            rec.state = "failed"
            rec.error = f"no agent factory for role {owner.value}"
            self.event_log.log_task_failed(
                mission_id=self.mission_id,
                task_id=rec.task.task_id,
                actor=owner.value,
                reason=rec.error,
                will_retry=False,
            )
            self._active_by_role[owner] -= 1
            rec.done_event.set()
            return

        # Cost-conscious mode (soul.md §5.5) caps retries to curb spend.
        retry_budget = rec.task.failure_handling.retry_budget
        if cost_conscious:
            retry_budget = min(retry_budget, _COST_CONSCIOUS_RETRY_CAP)
        max_attempts = max(1, 1 + retry_budget)
        result: AgentResult[Any] | None = None
        t0 = time.monotonic()
        for attempt in range(1, max_attempts + 1):
            rec.attempts = attempt
            try:
                result = await agent.run(
                    rec.task,
                    mission_id=self.mission_id,
                    coder_provider_in_use=self.coder_provider_in_use,
                )
            except Exception as e:
                logger.exception("Scheduler: agent.run raised unexpectedly: %r", e)
                result = None
            if result is not None and not result.errored:
                break
            if attempt < max_attempts:
                self.event_log.log_task_failed(
                    mission_id=self.mission_id,
                    task_id=rec.task.task_id,
                    actor=owner.value,
                    reason=(result.error_reason if result else "agent.run raised") or "unknown",
                    will_retry=True,
                )
        duration = time.monotonic() - t0
        self._active_by_role[owner] -= 1
        if result is None or result.errored:
            rec.state = "failed"
            rec.error = (result.error_reason if result else "agent.run raised") or "failed"
            self.event_log.log_task_failed(
                mission_id=self.mission_id,
                task_id=rec.task.task_id,
                actor=owner.value,
                reason=rec.error,
                will_retry=False,
            )
        else:
            rec.state = "complete"
            rec.result = result
            self.event_log.log_task_complete(
                mission_id=self.mission_id,
                task_id=rec.task.task_id,
                actor=owner.value,
                duration_sec=duration,
            )
            # F2 / soul.md §3.5 — sync the异-provider anchor to the provider the
            # Coder ACTUALLY ran on. Validators depend on the Coder, so they are
            # dispatched after this and will then enforce异-provider against the
            # real provider, not the stale config-primary guess.
            if owner is Role.CODER_WORKER:
                self._reconcile_coder_provider(result)
            # Phase D §D4 — a completed behavior_validator task now has both
            # verdicts on disk; reconcile them and record the arbitration decision.
            if owner is Role.BEHAVIOR_VALIDATOR:
                self._arbitrate_completed_behavior(rec)
        rec.done_event.set()


__all__ = ["Scheduler", "TaskState"]
