"""BudgetGuard — Phase E §E5 budget-band tests.

Encodes WHY each band matters:
- 50% must only annotate (alert) — touching budget_mode here would prematurely
  degrade a mission that is still on plan.
- 80% must flip to cost_conscious so downstream enforcement (fewer parallel /
  cheaper model / fewer retries) can engage before the budget is blown.
- 100% must pause + escalate so the scheduler stops launching NEW work and a
  human is notified.
- 150% must force-escalate again — a runaway past the pause line.
- Crossing a band must be idempotent: re-running the hook on later ticks within
  the same band must NOT re-emit, or the user gets paged on every tick.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from maf_coder.blackboard import ArtifactStore
from maf_coder.blackboard.event_log import EventKind
from maf_coder.orchestrator.budget import (
    MODE_COST_CONSCIOUS,
    MODE_NORMAL,
    MODE_PAUSED,
    BudgetGuard,
    classify_band,
    read_budget_usd,
)
from maf_coder.orchestrator.supervisor import SupervisionContext
from maf_coder.schemas import MissionState

_BUDGET = 100.0  # full mission budget for these tests (via budget.yaml)


def _store(tmp_path: Path, *, budget_total: float | None = _BUDGET) -> ArtifactStore:
    store = ArtifactStore(tmp_path / "missions", "m-budget")
    store.save_mission_state(
        MissionState(mission_id="m-budget", started_at=datetime.now(UTC))
    )
    if budget_total is not None:
        store.write_text("budget.yaml", f"total_budget_usd: {budget_total}\n")
    return store


def _ctx(store: ArtifactStore, *, cost: float) -> SupervisionContext:
    return SupervisionContext(
        mission_id=store.mission_id,
        mission_state=store.load_mission_state(),
        elapsed_hours=1.0,
        total_cost_usd=cost,
        now=datetime.now(UTC),
        store=store,
        event_log=store.event_log(),
    )


def _kinds(store: ArtifactStore) -> list[str]:
    return [e.kind for e in store.event_log().iter_events()]


# -- Pure band classification ----------------------------------------------


@pytest.mark.parametrize(
    ("ratio", "pct", "mode"),
    [
        (0.10, 0.0, MODE_NORMAL),
        (0.50, 50.0, MODE_NORMAL),
        (0.79, 50.0, MODE_NORMAL),
        (0.80, 80.0, MODE_COST_CONSCIOUS),
        (0.99, 80.0, MODE_COST_CONSCIOUS),
        (1.00, 100.0, MODE_PAUSED),
        (1.49, 100.0, MODE_PAUSED),
        (1.50, 150.0, MODE_PAUSED),
        (3.00, 150.0, MODE_PAUSED),
    ],
)
def test_classify_band_table(ratio: float, pct: float, mode: str) -> None:
    d = classify_band(ratio)
    assert d.threshold_pct == pct
    assert d.target_mode == mode


def test_read_budget_precedence() -> None:
    assert read_budget_usd({"total_budget_usd": 200.0}) == 200.0
    # falls back to alert_threshold * factor (2x)
    assert read_budget_usd({"alert_threshold_usd": 50.0}) == 100.0
    # sane default when neither present
    assert read_budget_usd({}) == 100.0


# -- Band effects on a live ctx --------------------------------------------


@pytest.mark.asyncio
async def test_50pct_annotates_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    guard = BudgetGuard()
    await guard(_ctx(store, cost=55.0))  # 55%
    assert store.load_mission_state().budget_mode == MODE_NORMAL
    kinds = _kinds(store)
    assert EventKind.BUDGET_ALERT.value in kinds
    assert EventKind.BUDGET_MODE_CHANGED.value not in kinds
    assert EventKind.ESCALATION_TRIGGERED.value not in kinds


@pytest.mark.asyncio
async def test_80pct_switches_cost_conscious(tmp_path: Path) -> None:
    store = _store(tmp_path)
    guard = BudgetGuard()
    await guard(_ctx(store, cost=85.0))  # 85%
    assert store.load_mission_state().budget_mode == MODE_COST_CONSCIOUS
    kinds = _kinds(store)
    assert EventKind.BUDGET_ALERT.value in kinds
    assert EventKind.BUDGET_MODE_CHANGED.value in kinds
    assert EventKind.ESCALATION_TRIGGERED.value not in kinds


@pytest.mark.asyncio
async def test_100pct_pauses_and_escalates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    guard = BudgetGuard()
    await guard(_ctx(store, cost=105.0))  # 105%
    assert store.load_mission_state().budget_mode == MODE_PAUSED
    kinds = _kinds(store)
    assert EventKind.BUDGET_MODE_CHANGED.value in kinds
    assert EventKind.ESCALATION_TRIGGERED.value in kinds


@pytest.mark.asyncio
async def test_150pct_force_escalates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    guard = BudgetGuard()
    await guard(_ctx(store, cost=160.0))  # 160%
    assert store.load_mission_state().budget_mode == MODE_PAUSED
    escalations = [
        e for e in store.event_log().iter_events()
        if e.kind == EventKind.ESCALATION_TRIGGERED.value
    ]
    assert len(escalations) == 1
    assert "force" in escalations[0].payload["reason"]


# -- Idempotency: each band fires its effects exactly once ------------------


@pytest.mark.asyncio
async def test_band_idempotent_across_ticks(tmp_path: Path) -> None:
    """Re-running within the same 80% band must not re-emit alert/mode-change."""
    store = _store(tmp_path)
    guard = BudgetGuard()
    # First tick crosses 80%.
    await guard(_ctx(store, cost=85.0))
    # Subsequent ticks still in the 80% band — must be silent.
    await guard(_ctx(store, cost=88.0))
    await guard(_ctx(store, cost=90.0))
    alerts = [e for e in store.event_log().iter_events()
              if e.kind == EventKind.BUDGET_ALERT.value]
    mode_changes = [e for e in store.event_log().iter_events()
                    if e.kind == EventKind.BUDGET_MODE_CHANGED.value]
    assert len(alerts) == 1
    assert len(mode_changes) == 1


@pytest.mark.asyncio
async def test_crossing_each_band_once_in_sequence(tmp_path: Path) -> None:
    """Walking 55→85→105→160 crosses each band exactly once: 4 alerts, 3 mode
    transitions (50% does not change mode), 2 escalations (100% + 150%)."""
    store = _store(tmp_path)
    guard = BudgetGuard()
    for cost in (55.0, 85.0, 105.0, 160.0):
        await guard(_ctx(store, cost=cost))
    events = list(store.event_log().iter_events())
    alerts = [e for e in events if e.kind == EventKind.BUDGET_ALERT.value]
    modes = [e for e in events if e.kind == EventKind.BUDGET_MODE_CHANGED.value]
    escalations = [e for e in events if e.kind == EventKind.ESCALATION_TRIGGERED.value]
    assert [e.payload["threshold_pct"] for e in alerts] == [50.0, 80.0, 100.0, 150.0]
    assert [e.payload["to_mode"] for e in modes] == [
        MODE_COST_CONSCIOUS,
        MODE_PAUSED,
    ]
    # 100% and 150% each escalate once.
    assert len(escalations) == 2
    assert store.load_mission_state().budget_mode == MODE_PAUSED


@pytest.mark.asyncio
async def test_no_budget_yaml_uses_default(tmp_path: Path) -> None:
    """Absent budget.yaml → default budget (100). cost=85 → 85% → cost_conscious."""
    store = _store(tmp_path, budget_total=None)
    guard = BudgetGuard()
    await guard(_ctx(store, cost=85.0))
    assert store.load_mission_state().budget_mode == MODE_COST_CONSCIOUS


@pytest.mark.asyncio
async def test_under_50pct_is_silent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    guard = BudgetGuard()
    await guard(_ctx(store, cost=10.0))
    assert _kinds(store) == []
    assert store.load_mission_state().budget_mode == MODE_NORMAL
