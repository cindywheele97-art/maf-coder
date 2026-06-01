"""Stuck recovery — Phase E §E4 triage tests.

Encodes WHY each tier maps where:
- low risk is a transient stall → AUTO_RETRY (cheap, no human, no re-plan).
- medium risk is a wrong implementation path → REPLAN carrying the SAME
  implementation_path_issue token the D4 arbitration re-plan uses, so the
  orchestrator re-plan loop keys off one vocabulary regardless of source.
- high risk is unrecoverable by the agent → HUMAN_GATE escalation.
The triage must be a PURE function (table-testable); the caller emits the event.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maf_coder.blackboard.event_log import EventKind, EventLog
from maf_coder.orchestrator.recovery import (
    RecoveryAction,
    RiskTier,
    StuckTrigger,
    recover,
    triage,
)
from maf_coder.validators.arbitration import (
    IMPLEMENTATION_PATH_ISSUE_SIGNAL,
    REPLAN_RISK_LEVEL,
)

# -- Pure triage table ------------------------------------------------------


@pytest.mark.parametrize(
    ("risk", "action"),
    [
        (RiskTier.LOW, RecoveryAction.AUTO_RETRY),
        (RiskTier.MEDIUM, RecoveryAction.REPLAN),
        (RiskTier.HIGH, RecoveryAction.HUMAN_GATE),
    ],
)
def test_triage_tier_to_action(risk: RiskTier, action: RecoveryAction) -> None:
    decision = triage(StuckTrigger(kind="no_progress", risk=risk))
    assert decision.action is action


def test_medium_carries_replan_tokens() -> None:
    """The medium row must reuse D4's re-plan vocabulary exactly."""
    decision = triage(StuckTrigger(kind="wrong_path", risk=RiskTier.MEDIUM))
    assert decision.signal == IMPLEMENTATION_PATH_ISSUE_SIGNAL
    assert decision.risk_level == REPLAN_RISK_LEVEL


def test_low_and_high_carry_no_replan_tokens() -> None:
    low = triage(StuckTrigger(kind="x", risk=RiskTier.LOW))
    high = triage(StuckTrigger(kind="x", risk=RiskTier.HIGH))
    assert low.signal is None
    assert low.risk_level is None
    assert high.signal is None
    assert high.risk_level is None


# -- recover() emits the right event per tier -------------------------------


def _log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "events.jsonl")


def test_recover_low_emits_retry_signal(tmp_path: Path) -> None:
    log = _log(tmp_path)
    decision = recover(
        log,
        mission_id="m1",
        trigger=StuckTrigger(kind="stall", risk=RiskTier.LOW, task_id="t1"),
    )
    assert decision.action is RecoveryAction.AUTO_RETRY
    events = list(log.iter_events())
    assert len(events) == 1
    assert events[0].kind == EventKind.TASK_FAILED.value
    assert events[0].payload["will_retry"] is True
    assert events[0].task_id == "t1"


def test_recover_medium_emits_replan_arbitration(tmp_path: Path) -> None:
    log = _log(tmp_path)
    decision = recover(
        log,
        mission_id="m1",
        trigger=StuckTrigger(kind="wrong_path", risk=RiskTier.MEDIUM, task_id="t2"),
    )
    assert decision.action is RecoveryAction.REPLAN
    events = list(log.iter_events())
    assert len(events) == 1
    assert events[0].kind == EventKind.VALIDATOR_ARBITRATION.value
    assert events[0].payload["signal"] == IMPLEMENTATION_PATH_ISSUE_SIGNAL
    assert events[0].payload["risk_level"] == REPLAN_RISK_LEVEL
    assert events[0].payload["decision"] == RecoveryAction.REPLAN.value


def test_recover_high_emits_human_gate_escalation(tmp_path: Path) -> None:
    log = _log(tmp_path)
    decision = recover(
        log,
        mission_id="m1",
        trigger=StuckTrigger(kind="deadlock", risk=RiskTier.HIGH, task_id="t3"),
    )
    assert decision.action is RecoveryAction.HUMAN_GATE
    events = list(log.iter_events())
    assert len(events) == 1
    assert events[0].kind == EventKind.ESCALATION_TRIGGERED.value
    assert events[0].payload["target"] == "human_gate"
    assert events[0].task_id == "t3"


def test_recover_uses_only_existing_event_kinds(tmp_path: Path) -> None:
    """Across all tiers, no novel event kind is invented."""
    allowed = {
        EventKind.TASK_FAILED.value,
        EventKind.VALIDATOR_ARBITRATION.value,
        EventKind.ESCALATION_TRIGGERED.value,
    }
    log = _log(tmp_path)
    for risk in (RiskTier.LOW, RiskTier.MEDIUM, RiskTier.HIGH):
        recover(log, mission_id="m1", trigger=StuckTrigger(kind="k", risk=risk))
    assert {e.kind for e in log.iter_events()} <= allowed
