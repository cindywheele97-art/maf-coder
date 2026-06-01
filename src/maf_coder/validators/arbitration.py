"""Validator conflict arbitration (Phase D PR-D4).

D3 built the dual-validator *chain gate*: a behavior_validator task may run only
after its review_validator dependency produces a PASS verdict. This module sits
one level up — given the two verdicts of a coder/review/behavior grouping, it
decides what the orchestrator should DO when they (dis)agree.

The decision table (execution plan §2 PR-D4 + soul.md):

    | Review | Behavior | Decision                       |
    |--------|----------|--------------------------------|
    | PASS   | FAIL     | REPLAN_IMPLEMENTATION_PATH     | re-plan, risk=medium
    | FAIL   | —        | BEHAVIOR_BLOCKED               | behavior never ran (D3)
    | FAIL   | PASS     | HUMAN_GATE                     | near-impossible; force escalate
    | PASS   | PASS     | CHECKPOINT_CANDIDATE           | milestone checkpoint candidate

This module is deliberately PURE: verdicts in, decision out. It performs no I/O
beyond reading verdict files via the injected store, and it never mutates state
or emits events. The scheduler owns the side effects (events / escalation),
keeping the decision logic trivially unit-testable.
"""

from __future__ import annotations

from enum import Enum

from ..blackboard import ArtifactStore
from ..schemas import VerdictResult

# Risk level the re-plan signal carries (PASS+FAIL row). Kept here so the signal
# value lives next to the decision that produces it rather than as a scheduler
# magic string.
REPLAN_RISK_LEVEL = "medium"

# Signal string the re-plan event carries. Matches D3's blocked-path payload so
# downstream consumers (orchestrator re-plan loop) key off a single token across
# both the "behavior never ran" and "behavior ran and failed" paths.
IMPLEMENTATION_PATH_ISSUE_SIGNAL = "implementation_path_issue"


class ArbitrationDecision(str, Enum):
    """The four arbitration outcomes. str-valued so it serializes cleanly into
    event payloads without a custom encoder."""

    REPLAN_IMPLEMENTATION_PATH = "replan_implementation_path"
    BEHAVIOR_BLOCKED = "behavior_blocked"
    HUMAN_GATE = "human_gate"
    CHECKPOINT_CANDIDATE = "checkpoint_candidate"


def arbitrate(
    review_result: VerdictResult | None, behavior_result: VerdictResult | None
) -> ArbitrationDecision:
    """Pure decision function over the two verdict results.

    Args:
        review_result: the review_validator verdict, or None if no review verdict
            exists (treated as not-PASS → the behavior was never cleared to run).
        behavior_result: the behavior_validator verdict, or None if behavior never
            produced one (blocked by the chain gate).

    Rows:
        Review FAIL (or missing) → BEHAVIOR_BLOCKED (D3 already enforced the gate;
            arbitration just agrees that behavior should not have run). The one
            exception is the near-impossible FAIL+PASS, escalated to HUMAN_GATE.
        Review PASS + Behavior FAIL → REPLAN_IMPLEMENTATION_PATH.
        Review PASS + Behavior PASS → CHECKPOINT_CANDIDATE.
        Review PASS + Behavior missing → BEHAVIOR_BLOCKED (cleared to run but no
            verdict yet; nothing to arbitrate).
    """
    review_pass = review_result == VerdictResult.PASS
    behavior_pass = behavior_result == VerdictResult.PASS

    if not review_pass:
        # FAIL+PASS is the soul.md "should be near-impossible" contradiction:
        # behavior somehow passed despite review failing. Force-escalate.
        if behavior_result is not None and behavior_pass:
            return ArbitrationDecision.HUMAN_GATE
        return ArbitrationDecision.BEHAVIOR_BLOCKED

    # Review PASS from here.
    if behavior_result is None:
        return ArbitrationDecision.BEHAVIOR_BLOCKED
    if behavior_pass:
        return ArbitrationDecision.CHECKPOINT_CANDIDATE
    return ArbitrationDecision.REPLAN_IMPLEMENTATION_PATH


def check_validator_preconditions(
    store: ArtifactStore, *, review_task_id: str, behavior_task_id: str
) -> ArbitrationDecision:
    """Read the review + behavior verdicts from the store and arbitrate.

    Missing verdict files are treated as "no verdict" (None) rather than raising:
    a behavior task that was blocked by the chain gate legitimately has no
    behavior verdict on disk, and arbitration must still return a decision.

    This is the store-backed entry point the scheduler calls. The pure decision
    lives in `arbitrate`; this wrapper only resolves verdicts to results.
    """
    review_result = _load_review_result(store, review_task_id)
    behavior_result = _load_behavior_result(store, behavior_task_id)
    return arbitrate(review_result, behavior_result)


def _load_review_result(store: ArtifactStore, task_id: str) -> VerdictResult | None:
    try:
        verdict = store.load_review_verdict(task_id)
    except FileNotFoundError:
        return None
    return VerdictResult(verdict.result)


def _load_behavior_result(store: ArtifactStore, task_id: str) -> VerdictResult | None:
    try:
        verdict = store.load_behavior_verdict(task_id)
    except FileNotFoundError:
        return None
    return VerdictResult(verdict.result)


__all__ = [
    "IMPLEMENTATION_PATH_ISSUE_SIGNAL",
    "REPLAN_RISK_LEVEL",
    "ArbitrationDecision",
    "arbitrate",
    "check_validator_preconditions",
]
