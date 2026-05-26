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
from ..blackboard.event_log import EventLog
from ..models import ModelRouter
from ..sandbox import SandboxClient
from ..schemas import Role, Task

logger = logging.getLogger(__name__)

TaskState = Literal["pending", "ready", "active", "complete", "failed", "blocked"]

_PARALLEL_LIMIT = {
    Role.CODER_WORKER: 1,
    Role.BEHAVIOR_VALIDATOR: 1,
}
_PARALLEL_DEFAULT = 4


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

    def _is_ready(self, rec: _TaskRecord) -> bool:
        if rec.state != "pending":
            return False
        # All dependencies complete
        for dep_id in rec.task.depends_on:
            dep = self._tasks.get(dep_id)
            if dep is None or dep.state != "complete":
                return False
        # Slot available for this role
        owner = self._coerce_role(rec.task.owner)
        cap = _PARALLEL_LIMIT.get(owner, _PARALLEL_DEFAULT)
        return self._active_by_role[owner] < cap

    @staticmethod
    def _coerce_role(role: Any) -> Role:
        if isinstance(role, Role):
            return role
        return Role(role)

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
            async with self._lock:
                for rec in self._tasks.values():
                    if self._is_ready(rec):
                        rec.state = "active"
                        owner = self._coerce_role(rec.task.owner)
                        self._active_by_role[owner] += 1
                        t = asyncio.create_task(self._run_one(rec))
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

    async def _run_one(self, rec: _TaskRecord) -> None:
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

        max_attempts = max(1, 1 + rec.task.failure_handling.retry_budget)
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
        rec.done_event.set()


__all__ = ["Scheduler", "TaskState"]
