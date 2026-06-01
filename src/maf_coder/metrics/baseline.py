"""Health-metric baseline over missions (Build Plan §Phase G · G3).

Why this exists:
    Phase G asks for "a set of metrics you can later use to say v3.1 beat v3.0".
    This module derives them mechanically from the canonical event stream
    (`events.jsonl`) and runtime state (`mission_state.json`) that every mission
    already writes — no LLM calls, no mutation, fully reproducible.

The metrics (per the Build Plan):
    - first-pass rate      : missions whose validators passed without a second pass
    - final-pass rate      : missions that reached a successful completion
    - average cost (USD)    + median
    - average wall-clock (h)
    - human-intervention rate : missions that hit at least one Human Gate
    - PR-review pass rate   : OPTIONAL — inherently human-judged, so it is only
                              computed over missions the caller annotates.
    - routing savings (USD) : bonus, summed from Smart Router ROUTE_DECISION events

Everything is derived from real fields:
    MISSION_END.payload{result,total_wall_clock_hours}, VALIDATOR_VERDICT
    .payload{result,triggered_second_pass}, SECOND_PASS_TRIGGERED,
    VALIDATOR_CHAIN_BLOCKED, ESCALATION_TRIGGERED.payload{target},
    ROUTE_DECISION.payload{saved_vs_baseline_usd}, EventLog.total_cost_usd/tokens.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..blackboard import ArtifactStore
from ..blackboard.event_log import Event, EventKind
from ..schemas import VerdictResult

# Mission results that count as a successful completion (vs aborted / crashed /
# stopped / the dry-run variants). Kept here so "what counts as a pass" is one
# obvious constant rather than scattered string checks.
_SUCCESS_RESULTS = frozenset({"complete", "resumed_complete"})

_HUMAN_GATE_TARGET = "human_gate"


class MissionMetrics(BaseModel):
    """Derived metrics for a single mission."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    result: str = Field(description="MISSION_END result, or '' if the mission never ended")
    completed: bool = Field(description="result is a successful completion")
    has_validator_verdicts: bool = Field(
        description="At least one VALIDATOR_VERDICT was recorded (first_pass is "
        "only meaningful when this is True)."
    )
    first_pass: bool = Field(
        description="Validators passed with no second pass / FAIL / chain block."
    )
    cost_usd: float
    wall_clock_hours: float
    tokens_in: int
    tokens_out: int
    validator_pass_count: int
    validator_fail_count: int
    second_pass_count: int
    chain_block_count: int
    human_gate_escalations: int
    human_intervention: bool
    routing_savings_usd: float
    pr_review_passed: bool | None = Field(
        default=None,
        description="Human judgement; None unless the caller annotated this mission.",
    )


class BaselineReport(BaseModel):
    """Aggregate health metrics across a set of missions."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    mission_count: int
    missions: list[MissionMetrics] = Field(default_factory=list)

    first_pass_rate: float = Field(description="Over missions that have validator verdicts.")
    final_pass_rate: float = Field(description="Over all missions.")
    avg_cost_usd: float
    median_cost_usd: float
    avg_wall_clock_hours: float
    human_intervention_rate: float
    pr_review_pass_rate: float | None = Field(
        default=None,
        description="Over annotated missions only; None when none were annotated.",
    )
    total_routing_savings_usd: float


def discover_missions(missions_root: str | Path) -> list[str]:
    """List mission ids under ``missions_root``.

    A directory is a mission iff it contains an `events.jsonl` or a
    `mission_state.json` — so stray directories are ignored. Sorted for
    deterministic output.
    """
    root = Path(missions_root)
    if not root.is_dir():
        return []
    ids = [
        p.name
        for p in root.iterdir()
        if p.is_dir() and ((p / "events.jsonl").exists() or (p / "mission_state.json").exists())
    ]
    return sorted(ids)


def compute_mission_metrics(
    store: ArtifactStore, *, pr_review_passed: bool | None = None
) -> MissionMetrics:
    """Derive metrics for one mission from its event log + state.

    Tolerant of partial missions: a mission that is still running (no
    MISSION_END) yields ``result=""`` / ``completed=False`` rather than raising.
    """
    event_log = store.event_log()
    events: list[Event] = list(event_log.iter_events())

    result = _last_mission_result(events)
    pass_count, fail_count = _validator_tallies(events)
    has_verdicts = (pass_count + fail_count) > 0
    second_pass_count = _count_kind(events, EventKind.SECOND_PASS_TRIGGERED)
    chain_block_count = _count_kind(events, EventKind.VALIDATOR_CHAIN_BLOCKED)
    human_gate = _human_gate_escalations(events)

    # First pass = validators ran and were clean on the first attempt: no FAIL,
    # no second-pass trigger, no chain block. Requires verdicts to be meaningful.
    first_pass = (
        has_verdicts
        and fail_count == 0
        and second_pass_count == 0
        and chain_block_count == 0
    )

    tokens_in, tokens_out = event_log.total_tokens()

    return MissionMetrics(
        mission_id=store.mission_id,
        result=result,
        completed=result in _SUCCESS_RESULTS,
        has_validator_verdicts=has_verdicts,
        first_pass=first_pass,
        cost_usd=_mission_cost(store, event_log, events),
        wall_clock_hours=_mission_wall_clock(store, events),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        validator_pass_count=pass_count,
        validator_fail_count=fail_count,
        second_pass_count=second_pass_count,
        chain_block_count=chain_block_count,
        human_gate_escalations=human_gate,
        human_intervention=human_gate > 0,
        routing_savings_usd=_routing_savings(events),
        pr_review_passed=pr_review_passed,
    )


def compute_baseline(
    missions_root: str | Path,
    *,
    mission_ids: list[str] | None = None,
    pr_review_passed: dict[str, bool] | None = None,
) -> BaselineReport:
    """Aggregate metrics across missions under ``missions_root``.

    ``mission_ids`` defaults to every discovered mission. ``pr_review_passed``
    maps mission_id → whether a human approved its PR (the one metric that
    cannot be derived); missions absent from the map are left unannotated.
    """
    ids = mission_ids if mission_ids is not None else discover_missions(missions_root)
    annotations = pr_review_passed or {}

    missions = [
        compute_mission_metrics(
            ArtifactStore(missions_root, mid),
            pr_review_passed=annotations.get(mid),
        )
        for mid in ids
    ]

    n = len(missions)
    costs = [m.cost_usd for m in missions]
    with_verdicts = [m for m in missions if m.has_validator_verdicts]
    annotated = [m for m in missions if m.pr_review_passed is not None]

    return BaselineReport(
        mission_count=n,
        missions=missions,
        first_pass_rate=_rate(sum(m.first_pass for m in with_verdicts), len(with_verdicts)),
        final_pass_rate=_rate(sum(m.completed for m in missions), n),
        avg_cost_usd=statistics.fmean(costs) if costs else 0.0,
        median_cost_usd=statistics.median(costs) if costs else 0.0,
        avg_wall_clock_hours=(
            statistics.fmean([m.wall_clock_hours for m in missions]) if n else 0.0
        ),
        human_intervention_rate=_rate(sum(m.human_intervention for m in missions), n),
        pr_review_pass_rate=(
            _rate(sum(bool(m.pr_review_passed) for m in annotated), len(annotated))
            if annotated
            else None
        ),
        total_routing_savings_usd=sum(m.routing_savings_usd for m in missions),
    )


def render_baseline_markdown(report: BaselineReport) -> str:
    """Render a human-readable baseline summary."""
    pr_line = (
        f"{report.pr_review_pass_rate:.0%}"
        if report.pr_review_pass_rate is not None
        else "n/a (no missions annotated)"
    )
    lines = [
        "# MAF-Coder — Health Metric Baseline",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        f"Missions analyzed: **{report.mission_count}**",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| First-pass rate | {report.first_pass_rate:.0%} |",
        f"| Final-pass rate | {report.final_pass_rate:.0%} |",
        f"| Human-intervention rate | {report.human_intervention_rate:.0%} |",
        f"| PR-review pass rate | {pr_line} |",
        f"| Avg cost (USD) | ${report.avg_cost_usd:.2f} |",
        f"| Median cost (USD) | ${report.median_cost_usd:.2f} |",
        f"| Avg wall-clock (h) | {report.avg_wall_clock_hours:.2f} |",
        f"| Routing savings (USD) | ${report.total_routing_savings_usd:.2f} |",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Derivation helpers (each keyed off one real event/field)
# ---------------------------------------------------------------------------


def _last_mission_result(events: list[Event]) -> str:
    """The result of the last MISSION_END event (a mission may end more than
    once across resume); '' if it never ended."""
    result = ""
    for e in events:
        if e.kind == EventKind.MISSION_END.value:
            result = str(e.payload.get("result", ""))
    return result


def _validator_tallies(events: list[Event]) -> tuple[int, int]:
    """(pass_count, fail_count) over VALIDATOR_VERDICT events."""
    passes = fails = 0
    for e in events:
        if e.kind != EventKind.VALIDATOR_VERDICT.value:
            continue
        if str(e.payload.get("result", "")) == VerdictResult.PASS.value:
            passes += 1
        else:
            fails += 1
    return passes, fails


def _count_kind(events: list[Event], kind: EventKind) -> int:
    return sum(1 for e in events if e.kind == kind.value)


def _human_gate_escalations(events: list[Event]) -> int:
    return sum(
        1
        for e in events
        if e.kind == EventKind.ESCALATION_TRIGGERED.value
        and str(e.payload.get("target", "")) == _HUMAN_GATE_TARGET
    )


def _routing_savings(events: list[Event]) -> float:
    total = 0.0
    for e in events:
        if e.kind == EventKind.ROUTE_DECISION.value:
            saved = e.payload.get("saved_vs_baseline_usd")
            if isinstance(saved, (int, float)):
                total += float(saved)
    return total


def _mission_cost(store: ArtifactStore, event_log: object, events: list[Event]) -> float:
    """Total cost: the EventLog's summed LLM-call cost is canonical. Fall back
    to mission_state.cumulative_cost_usd (the heartbeat mirror) when the log has
    no cost events but state recorded some."""
    cost = event_log.total_cost_usd()  # type: ignore[attr-defined]
    if cost > 0:
        return float(cost)
    try:
        return float(store.load_mission_state().cumulative_cost_usd)
    except (FileNotFoundError, ValueError):
        return float(cost)


def _mission_wall_clock(store: ArtifactStore, events: list[Event]) -> float:
    """Wall-clock hours: prefer the MISSION_END payload (authoritative at end),
    else mission_state.cumulative_wall_clock_hours (heartbeat mirror)."""
    for e in reversed(events):
        if e.kind == EventKind.MISSION_END.value:
            val = e.payload.get("total_wall_clock_hours")
            if isinstance(val, (int, float)):
                return float(val)
            break
    try:
        return float(store.load_mission_state().cumulative_wall_clock_hours)
    except (FileNotFoundError, ValueError):
        return 0.0


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0
