"""MissionSupervisor — the Phase E supervision spine (Build Plan §Phase E).

Why this exists:
    A multi-day mission needs a heartbeat that runs *concurrently* with the
    scheduler's execution loop: something that periodically refreshes the
    mission's runtime state and gives later workstreams (status-report timer,
    budget guard, stuck-recovery) a single place to plug a hook into.

    This module is deliberately minimal: it is the socket, not the appliance.
    Status reports, budget enforcement, stuck recovery and resume are later
    workstreams (E-comms / E-guard / E-state). Each registers a
    ``SupervisionHook`` against the contract below.

Design:
    - ``MissionSupervisor.run(stop_event)`` ticks every ``tick_interval_sec``
      until ``stop_event`` is set, then returns cleanly.
    - Each tick reloads ``mission_state`` fresh, builds a ``SupervisionContext``
      and invokes every registered hook. A hook that raises is isolated: it is
      logged and never prevents the other hooks from running or escapes the
      loop. A supervisor failure must never change the mission result.
    - One built-in hook, ``heartbeat``, refreshes
      ``cumulative_wall_clock_hours`` / ``cumulative_cost_usd`` and persists
      ``mission_state``. It is the reference pattern for later hooks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ..blackboard import ArtifactStore
from ..blackboard.event_log import EventLog
from ..schemas import MissionState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SupervisionContext:
    """Immutable snapshot handed to each hook on every tick.

    ``mission_state`` is freshly loaded each tick; hooks must not assume it is
    shared across ticks. ``now`` is tz-aware UTC.
    """

    mission_id: str
    mission_state: MissionState
    elapsed_hours: float
    total_cost_usd: float
    now: datetime
    store: ArtifactStore
    event_log: EventLog


SupervisionHook = Callable[[SupervisionContext], Awaitable[None]]


async def heartbeat(ctx: SupervisionContext) -> None:
    """Built-in hook: refresh runtime counters on ``mission_state`` and persist.

    Pure state refresh — no external effects. Serves as the reference pattern
    for Wave 2 hooks: read from ``ctx``, produce a new immutable ``MissionState``
    (never mutate in place), persist via the store.
    """
    refreshed = ctx.mission_state.model_copy(
        update={
            "cumulative_wall_clock_hours": ctx.elapsed_hours,
            "cumulative_cost_usd": ctx.total_cost_usd,
        }
    )
    ctx.store.save_mission_state(refreshed)


class MissionSupervisor:
    """Concurrent supervision loop. Owns the tick cadence and the hook list."""

    def __init__(
        self,
        *,
        store: ArtifactStore,
        event_log: EventLog,
        mission_id: str,
        started_at: datetime,
        tick_interval_sec: float = 60.0,
    ) -> None:
        self.store = store
        self.event_log = event_log
        self.mission_id = mission_id
        self.started_at = started_at
        self.tick_interval_sec = tick_interval_sec
        self._hooks: list[SupervisionHook] = [heartbeat]

    def register(self, hook: SupervisionHook) -> None:
        """Append a hook. Hooks run in registration order on every tick."""
        self._hooks.append(hook)

    async def tick_once(self) -> None:
        """Run one supervision tick: reload state, build context, run hooks.

        FileNotFoundError-safe: if ``mission_state.json`` is missing (e.g. the
        very first ticks race state initialization), the tick is skipped
        gracefully. Each hook is wrapped so a raising hook is isolated.
        """
        try:
            mission_state = self.store.load_mission_state()
        except FileNotFoundError:
            logger.debug("MissionSupervisor: mission_state not found yet — skipping tick")
            return

        now = datetime.now(UTC)
        ctx = SupervisionContext(
            mission_id=self.mission_id,
            mission_state=mission_state,
            elapsed_hours=self._elapsed_hours(now),
            total_cost_usd=self.event_log.total_cost_usd(),
            now=now,
            store=self.store,
            event_log=self.event_log,
        )

        for hook in self._hooks:
            try:
                await hook(ctx)
            except Exception as e:
                # A hook that raises must never crash the mission or stop the
                # other hooks. Log it; there is no clean existing EventKind for
                # "supervision hook failed" and the spec says not to invent a
                # noisy new kind, so we keep this to the logger.
                logger.exception(
                    "MissionSupervisor: hook %r raised — isolated, continuing: %r",
                    getattr(hook, "__name__", hook),
                    e,
                )

    async def run(self, stop_event: asyncio.Event) -> None:
        """Tick loop. Ticks once, then waits up to ``tick_interval_sec`` or until
        ``stop_event`` is set, whichever comes first. Returns cleanly when
        ``stop_event`` is set. Never raises out of the loop for hook errors.
        """
        while not stop_event.is_set():
            await self.tick_once()
            if stop_event.is_set():
                return
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.tick_interval_sec
                )
            except TimeoutError:
                # Interval elapsed without a stop request — run the next tick.
                continue

    def _elapsed_hours(self, now: datetime) -> float:
        return (now - self.started_at).total_seconds() / 3600.0


__all__ = ["MissionSupervisor", "SupervisionContext", "SupervisionHook", "heartbeat"]
