"""Stuck recovery — Phase E §E4 three-tier triage.

Why this exists:
    A multi-day mission can stall: a worker loops, a dependency never resolves,
    a plan path is wrong. Rather than wedging, the orchestrator triages the
    stuck condition into a risk tier and takes a graduated action:

        | risk tier | action                          | mechanism                  |
        |-----------|---------------------------------|----------------------------|
        | low       | AUTO_RETRY                      | retry signal               |
        | medium    | REPLAN (implementation_path)    | orchestrator re-plan signal|
        | high      | HUMAN_GATE                      | human-gate escalation      |

Design:
    The triage itself is a PURE function (``triage``): trigger + context in,
    ``RecoveryDecision`` out. No I/O, no events — trivially table-testable. The
    thin ``recover`` caller emits the right event for the decision (reusing the
    existing EventKinds and the D4 re-plan token vocabulary). This mirrors the
    arbitration module's pure-decision / side-effects-in-caller split.

Integration point:
    A live stuck-DETECTION loop (no progress over N ticks) is intentionally NOT
    wired into the scheduler here — that is a conservative, separately-reviewed
    change. ``StuckTrigger`` + ``triage`` + ``recover`` give the orchestrator a
    ready socket: when detection fires (manually or from a future no-progress
    hook), it builds a ``StuckTrigger`` and calls ``recover``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from ..blackboard.event_log import EventLog
from ..validators.arbitration import (
    IMPLEMENTATION_PATH_ISSUE_SIGNAL,
    REPLAN_RISK_LEVEL,
)

logger = logging.getLogger(__name__)


class RiskTier(str, Enum):
    """The stuck-condition risk tier. str-valued for clean event serialization."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecoveryAction(str, Enum):
    """The action a tier maps to."""

    AUTO_RETRY = "auto_retry"
    REPLAN = "replan"
    HUMAN_GATE = "human_gate"


@dataclass(frozen=True)
class StuckTrigger:
    """An observed stuck condition handed to the triage function.

    ``risk`` is the caller's assessment of how serious the stall is. ``kind`` is
    a short label (e.g. "no_progress", "retry_exhausted", "dependency_deadlock")
    used only for the event payload / forensics. ``task_id`` is the stalled task
    when one is identifiable.
    """

    kind: str
    risk: RiskTier
    task_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class RecoveryDecision:
    """Pure triage output: the chosen action + the signal/risk tokens it carries.

    ``signal`` carries the D4 ``implementation_path_issue`` token on the REPLAN
    row so the orchestrator re-plan loop keys off a single vocabulary across both
    validator-arbitration re-plans and stuck-recovery re-plans. ``risk_level`` is
    the re-plan risk (medium) on that row. Both are None on non-replan rows.
    """

    action: RecoveryAction
    signal: str | None = None
    risk_level: str | None = None


# Pure tier → action table. Lives next to triage so the mapping is one glance.
_TIER_ACTION: dict[RiskTier, RecoveryAction] = {
    RiskTier.LOW: RecoveryAction.AUTO_RETRY,
    RiskTier.MEDIUM: RecoveryAction.REPLAN,
    RiskTier.HIGH: RecoveryAction.HUMAN_GATE,
}


def triage(trigger: StuckTrigger) -> RecoveryDecision:
    """Pure: map a stuck trigger's risk tier to a recovery decision.

    low    → AUTO_RETRY
    medium → REPLAN, carrying the implementation_path_issue signal + medium risk
             (reuses D4's re-plan token vocabulary)
    high   → HUMAN_GATE
    """
    action = _TIER_ACTION[trigger.risk]
    if action is RecoveryAction.REPLAN:
        return RecoveryDecision(
            action=action,
            signal=IMPLEMENTATION_PATH_ISSUE_SIGNAL,
            risk_level=REPLAN_RISK_LEVEL,
        )
    return RecoveryDecision(action=action)


def recover(
    event_log: EventLog,
    *,
    mission_id: str,
    trigger: StuckTrigger,
) -> RecoveryDecision:
    """Triage the trigger, emit the matching event, return the decision.

    Thin side-effecting caller over the pure ``triage``:
      - AUTO_RETRY  → TASK_FAILED(will_retry=True) — the existing retry signal.
      - REPLAN      → VALIDATOR_ARBITRATION carrying the implementation_path_issue
                      signal + medium risk (same token the D4 re-plan loop reads).
      - HUMAN_GATE  → log_escalation(target="human_gate") — the existing path.
    Reuses existing EventKinds only; invents no new noisy kind.
    """
    decision = triage(trigger)
    if decision.action is RecoveryAction.AUTO_RETRY:
        event_log.log_task_failed(
            mission_id=mission_id,
            task_id=trigger.task_id or "unknown",
            actor="orchestrator",
            reason=f"stuck:{trigger.kind} — auto-retry ({trigger.detail})".rstrip(" ()—"),
            will_retry=True,
        )
    elif decision.action is RecoveryAction.REPLAN:
        event_log.log_validator_arbitration(
            mission_id=mission_id,
            behavior_task_id=trigger.task_id or "unknown",
            review_task_id=None,
            decision=RecoveryAction.REPLAN.value,
            signal=decision.signal,
            risk_level=decision.risk_level,
        )
    else:  # HUMAN_GATE
        event_log.log_escalation(
            mission_id=mission_id,
            target="human_gate",
            reason=f"stuck:{trigger.kind} — high risk ({trigger.detail})".rstrip(" ()—"),
            task_id=trigger.task_id,
        )
    return decision


__all__ = [
    "RecoveryAction",
    "RecoveryDecision",
    "RiskTier",
    "StuckTrigger",
    "recover",
    "triage",
]
