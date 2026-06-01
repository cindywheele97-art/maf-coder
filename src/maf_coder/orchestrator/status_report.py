"""StatusReport assembly + emission as a SupervisionHook (Phase E E-comms / E2).

Why this exists:
    soul.md §5.2 mandates a periodic 4-8h sync to the user. This module turns
    that requirement into a ``SupervisionHook``: on each supervision tick it
    checks whether a report is *due* (configurable interval since the last one),
    and if so assembles a ``StatusReport`` from live mission state + the event
    log, renders BOTH the human ``.md`` and machine ``.json`` (via the existing
    ``ArtifactStore.save_status_report``), updates
    ``mission_state.last_status_report_at`` immutably, emits a
    ``STATUS_REPORT_EMITTED`` event, then hands the report to a push adapter.

    It MUST NOT block: it runs on the supervisor, which is already concurrent
    with the scheduler. Push errors are swallowed so delivery never affects the
    mission result.

Design:
    - ``make_status_report_hook(interval, push)`` returns the hook closure so the
      interval and push adapter are injectable (small interval in tests).
    - ``DEFAULT_STATUS_INTERVAL`` = 4h matches the lower bound of the soul.md
      4-8h window.
    - Report number = (count of existing status_*.json) + 1, so numbering is
      derived from disk and survives restarts.
    - Budget derivation mirrors the ``get_budget_status`` tool: cost + tokens
      from the EventLog, ``alert_threshold_usd`` from ``budget.yaml`` (default
      50.0), linear projection.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from ..schemas import BudgetStatus, MilestoneStatus, StatusReport
from .push import NullPushAdapter, PushAdapter
from .supervisor import SupervisionContext, SupervisionHook

logger = logging.getLogger(__name__)

# Lower bound of the soul.md §5.2 4-8h sync window. Injectable per-mission /
# per-test via make_status_report_hook(interval=...).
DEFAULT_STATUS_INTERVAL = timedelta(hours=4)

_DEFAULT_ALERT_THRESHOLD_USD = 50.0


def _next_report_number(ctx: SupervisionContext) -> int:
    """Next report number = count of existing status_*.json on disk + 1.

    Derived from disk (not mission_state) so numbering is correct after a
    resume and never collides with an already-written report.
    """
    existing = [
        p
        for p in ctx.store.list_dir("status_reports")
        if p.is_file() and p.name.startswith("status_") and p.suffix == ".json"
    ]
    return len(existing) + 1


def _milestone_statuses(ctx: SupervisionContext) -> list[MilestoneStatus]:
    """Map mission_state milestones to MilestoneStatus.

    Completed milestones -> 'complete'; the current one -> 'in_progress'.
    """
    ms = ctx.mission_state
    statuses = [
        MilestoneStatus(milestone_id=m, state="complete") for m in ms.completed_milestones
    ]
    if ms.current_milestone is not None and ms.current_milestone not in ms.completed_milestones:
        statuses.append(
            MilestoneStatus(milestone_id=ms.current_milestone, state="in_progress")
        )
    return statuses


def _budget_status(ctx: SupervisionContext) -> BudgetStatus:
    """Derive the budget snapshot from the EventLog — mirrors get_budget_status."""
    cost = ctx.event_log.total_cost_usd()
    tokens_in, tokens_out = ctx.event_log.total_tokens()
    try:
        budget_cfg = ctx.store.read_yaml("budget.yaml")
    except Exception:
        budget_cfg = {}
    alert_threshold = float(budget_cfg.get("alert_threshold_usd", _DEFAULT_ALERT_THRESHOLD_USD))
    return BudgetStatus(
        tokens_used=tokens_in + tokens_out,
        cost_usd=cost,
        alert_threshold_usd=alert_threshold,
        # Linear projection from current burn (no time-window known here),
        # matching the get_budget_status tool's naive derivation.
        projected_total_usd=cost,
        wall_clock_vs_estimate_pct=100.0,
    )


def _current_activity(ctx: SupervisionContext) -> str:
    """Best-effort human description of what the mission is doing now."""
    ms = ctx.mission_state
    if ms.current_milestone is not None:
        return f"Working on milestone {ms.current_milestone}"
    if ms.completed_milestones:
        return f"Completed {len(ms.completed_milestones)} milestone(s); awaiting next"
    return "Mission in progress"


def _assemble_report(ctx: SupervisionContext, report_number: int) -> StatusReport:
    return StatusReport(
        report_number=report_number,
        mission_id=ctx.mission_id,
        mission_started_at=ctx.mission_state.started_at,
        elapsed_hours=ctx.elapsed_hours,
        milestones=_milestone_statuses(ctx),
        current_activity=_current_activity(ctx),
        budget_status=_budget_status(ctx),
    )


def _is_due(ctx: SupervisionContext, interval: timedelta) -> bool:
    """A report is due if it has never been emitted, or interval has elapsed."""
    last = ctx.mission_state.last_status_report_at
    if last is None:
        return True
    return (ctx.now - last) >= interval


def make_status_report_hook(
    *,
    interval: timedelta = DEFAULT_STATUS_INTERVAL,
    push: PushAdapter | None = None,
) -> SupervisionHook:
    """Build the status-report SupervisionHook.

    ``interval`` controls how often a report fires (default 4h; pass a small
    timedelta in tests). ``push`` is the delivery adapter (default Null).
    """
    adapter = push or NullPushAdapter()

    async def status_report_hook(ctx: SupervisionContext) -> None:
        if not _is_due(ctx, interval):
            return

        report_number = _next_report_number(ctx)
        report = _assemble_report(ctx, report_number)

        # Render both human .md and machine .json (existing store helper).
        ctx.store.save_status_report(report)

        # Persist last_status_report_at immutably, mirroring the heartbeat pattern.
        refreshed = ctx.mission_state.model_copy(
            update={"last_status_report_at": ctx.now}
        )
        ctx.store.save_mission_state(refreshed)

        ctx.event_log.log_status_report_emitted(
            mission_id=ctx.mission_id,
            report_number=report.report_number,
            cost_usd=report.budget_status.cost_usd,
            elapsed_hours=report.elapsed_hours,
        )

        # Push out of band — best-effort, never block / propagate.
        try:
            await adapter.send(report)
        except Exception as e:
            logger.warning(
                "status_report_hook: push adapter failed for report #%d: %r",
                report.report_number,
                e,
            )

    return status_report_hook


__all__ = ["DEFAULT_STATUS_INTERVAL", "make_status_report_hook"]
