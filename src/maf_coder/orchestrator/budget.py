"""Budget guard — Phase E §E5 SupervisionHook.

Why this exists:
    A multi-day mission must not silently burn past its budget. This hook runs
    on every supervision tick (see ``supervisor.py``), compares the EventLog-
    derived spend against the mission budget, and acts at four bands:

        | band | action                                                        |
        |------|---------------------------------------------------------------|
        |  50% | BUDGET_ALERT (annotate only — no mode change)                 |
        |  80% | BUDGET_ALERT + budget_mode → "cost_conscious"                 |
        | 100% | budget_mode → "paused" + escalation; scheduler stops NEW work |
        | 150% | "paused" + force-escalate to the human gate                   |

    "cost_conscious" is a *recorded state flag*: the actual enforcement (fewer
    parallel workers / cheaper model / fewer retries) is consumed elsewhere and
    is a documented TODO. This hook only sets the flag + emits the event.

Idempotency:
    Bands are crossed exactly once. The hook keys off the band already implied
    by ``mission_state.budget_mode`` plus a per-hook "highest alert band seen"
    memo, so a band's events/transition do not re-fire on every subsequent tick
    once the mission is already in (or past) that band.

Contract:
    A ``BudgetGuard`` instance is a ``SupervisionHook`` (it is callable as
    ``async def __call__(ctx)``). It reads ``ctx``, produces a new immutable
    ``MissionState`` via ``model_copy`` when the mode changes, and persists it —
    exactly the heartbeat reference pattern. It never mutates state in place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..schemas import MissionState
from .supervisor import SupervisionContext

logger = logging.getLogger(__name__)

# Fallback budget if budget.yaml has neither total_budget_usd nor
# alert_threshold_usd: a sane non-zero default so the guard never divides by
# zero and never treats every mission as instantly over-budget.
_DEFAULT_BUDGET_USD = 100.0

# When only alert_threshold_usd is present, treat it as a fraction of the full
# budget. The alert threshold is conventionally the 50% annotate band (see
# make_get_budget_status, which flips to cost_conscious at alert_threshold), so
# the implied full budget is roughly 2x the alert threshold.
_ALERT_TO_BUDGET_FACTOR = 2.0

# Budget modes (mirror MissionState.budget_mode vocabulary).
MODE_NORMAL = "normal"
MODE_COST_CONSCIOUS = "cost_conscious"
MODE_PAUSED = "paused"

# Band thresholds as fractions of the full budget, in ascending order.
_BAND_50 = 0.50
_BAND_80 = 0.80
_BAND_100 = 1.00
_BAND_150 = 1.50


@dataclass(frozen=True)
class BudgetDecision:
    """Pure classification of a spend ratio into a band + intended effects."""

    threshold_pct: float  # band crossed, e.g. 50.0/80.0/100.0/150.0; 0 if none
    target_mode: str  # budget_mode this band implies
    alert: bool  # emit a BUDGET_ALERT
    escalate: bool  # emit a human-gate escalation
    force: bool  # 150% force band (escalation reason notes force)


def read_budget_usd(event_log_budget_cfg: dict[str, object]) -> float:
    """Resolve the full mission budget (USD) from a budget.yaml-shaped dict.

    Precedence: ``total_budget_usd`` → ``alert_threshold_usd`` * factor →
    sane default. Mirrors make_get_budget_status's budget.yaml reading.
    """
    total = event_log_budget_cfg.get("total_budget_usd")
    if total is not None:
        try:
            value = float(total)  # type: ignore[arg-type]
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    alert = event_log_budget_cfg.get("alert_threshold_usd")
    if alert is not None:
        try:
            value = float(alert) * _ALERT_TO_BUDGET_FACTOR  # type: ignore[arg-type]
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return _DEFAULT_BUDGET_USD


def classify_band(ratio: float) -> BudgetDecision:
    """Pure: map a spend/budget ratio to the highest band it has reached.

    Returns the *highest* band crossed (so a tick that jumps straight from
    normal to 120% reports the 100% band's effects). ``threshold_pct == 0``
    means below the lowest band → no action.
    """
    if ratio >= _BAND_150:
        return BudgetDecision(
            threshold_pct=150.0,
            target_mode=MODE_PAUSED,
            alert=True,
            escalate=True,
            force=True,
        )
    if ratio >= _BAND_100:
        return BudgetDecision(
            threshold_pct=100.0,
            target_mode=MODE_PAUSED,
            alert=True,
            escalate=True,
            force=False,
        )
    if ratio >= _BAND_80:
        return BudgetDecision(
            threshold_pct=80.0,
            target_mode=MODE_COST_CONSCIOUS,
            alert=True,
            escalate=False,
            force=False,
        )
    if ratio >= _BAND_50:
        return BudgetDecision(
            threshold_pct=50.0,
            target_mode=MODE_NORMAL,  # annotate only — no mode change
            alert=True,
            escalate=False,
            force=False,
        )
    return BudgetDecision(
        threshold_pct=0.0,
        target_mode=MODE_NORMAL,
        alert=False,
        escalate=False,
        force=False,
    )


class BudgetGuard:
    """SupervisionHook that enforces the four-band budget policy (§E5).

    Stateful only for idempotency: ``_last_alert_pct`` remembers the highest
    band whose BUDGET_ALERT we already emitted, so we alert once per band even
    though the hook is re-invoked every tick. Mode transitions are additionally
    guarded by the persisted ``mission_state.budget_mode`` so they are idempotent
    across process restarts too.
    """

    def __init__(self) -> None:
        # Highest alert band already emitted (0 = none yet). In-process memo;
        # the persisted budget_mode is the durable idempotency anchor for the
        # mode transitions themselves.
        self._last_alert_pct: float = 0.0
        # Highest escalation band already emitted (0 = none yet). 100% and 150%
        # each escalate exactly once; this memo makes that idempotent without
        # coupling to the paused-mode transition timing.
        self._last_escalation_pct: float = 0.0

    async def __call__(self, ctx: SupervisionContext) -> None:
        budget = self._resolve_budget(ctx)
        if budget <= 0:
            return
        cost = ctx.total_cost_usd
        ratio = cost / budget
        decision = classify_band(ratio)
        if decision.threshold_pct == 0.0:
            return

        # -- Alert: once per band ------------------------------------------
        if decision.alert and decision.threshold_pct > self._last_alert_pct:
            ctx.event_log.log_budget_alert(
                mission_id=ctx.mission_id,
                threshold_pct=decision.threshold_pct,
                cost_usd=cost,
                budget_usd=budget,
            )
            self._last_alert_pct = decision.threshold_pct

        # -- Mode transition: only when the persisted mode must change -----
        current_mode = ctx.mission_state.budget_mode
        if decision.target_mode != MODE_NORMAL and current_mode != decision.target_mode:
            self._transition_mode(
                ctx,
                from_mode=current_mode,
                to_mode=decision.target_mode,
                threshold_pct=decision.threshold_pct,
                cost=cost,
                budget=budget,
            )

        # -- Escalation: once per escalating band (100% and 150%) ----------
        # The 100% band escalates once; the 150% force band escalates again
        # (force=True). Memo keyed on the band so each fires exactly once even
        # though both share target_mode == "paused".
        if decision.escalate and decision.threshold_pct > self._last_escalation_pct:
            self._escalate(ctx, decision=decision, cost=cost, budget=budget)
            self._last_escalation_pct = decision.threshold_pct

    # -- Internals ---------------------------------------------------------

    def _resolve_budget(self, ctx: SupervisionContext) -> float:
        try:
            cfg = ctx.store.read_yaml("budget.yaml")
        except Exception:
            cfg = {}
        return read_budget_usd(cfg)

    def _transition_mode(
        self,
        ctx: SupervisionContext,
        *,
        from_mode: str,
        to_mode: str,
        threshold_pct: float,
        cost: float,
        budget: float,
    ) -> None:
        """Immutable model_copy + persist + BUDGET_MODE_CHANGED event."""
        updated: MissionState = ctx.mission_state.model_copy(
            update={"budget_mode": to_mode}
        )
        ctx.store.save_mission_state(updated)
        ctx.event_log.log_budget_mode_changed(
            mission_id=ctx.mission_id,
            from_mode=from_mode,
            to_mode=to_mode,
            threshold_pct=threshold_pct,
            cost_usd=cost,
            budget_usd=budget,
        )

    def _escalate(
        self,
        ctx: SupervisionContext,
        *,
        decision: BudgetDecision,
        cost: float,
        budget: float,
    ) -> None:
        reason = (
            f"budget {'force-' if decision.force else ''}exceeded: "
            f"cost=${cost:.2f} vs budget=${budget:.2f} "
            f"({decision.threshold_pct:.0f}% band)"
        )
        ctx.event_log.log_escalation(
            mission_id=ctx.mission_id,
            target="human_gate",
            reason=reason,
        )


def make_budget_guard() -> BudgetGuard:
    """Factory for the budget SupervisionHook (registered in mission_driver)."""
    return BudgetGuard()


__all__ = [
    "MODE_COST_CONSCIOUS",
    "MODE_NORMAL",
    "MODE_PAUSED",
    "BudgetDecision",
    "BudgetGuard",
    "classify_band",
    "make_budget_guard",
    "read_budget_usd",
]
