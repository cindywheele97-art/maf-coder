"""Tests for the G3 health-metric baseline harness.

Missions are built with the REAL ArtifactStore + EventLog writers, so these
tests double as a check that the harness reads the same fields the rest of the
system writes. Each test states WHY the metric behaves as asserted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from maf_coder.blackboard import ArtifactStore
from maf_coder.metrics import (
    compute_baseline,
    compute_mission_metrics,
    discover_missions,
    render_baseline_markdown,
)
from maf_coder.schemas import MissionState, VerdictResult


def _store(root: Path, mid: str, *, with_state: bool = True) -> ArtifactStore:
    store = ArtifactStore(root, mid)
    if with_state:
        store.save_mission_state(MissionState(mission_id=mid, started_at=datetime.now(UTC)))
    return store


def _clean_pass_mission(root: Path, mid: str, *, result: str = "complete") -> ArtifactStore:
    """A mission that completed with passing validators and no second pass."""
    store = _store(root, mid)
    log = store.event_log()
    log.log_mission_start(mission_id=mid, goal="add /health", repo="/repo")
    log.log_llm_call(
        mission_id=mid, actor="coder_worker", model="x", tokens_in=100, tokens_out=50,
        cost_usd=0.40, latency_sec=1.0,
    )
    log.log_validator_verdict(
        mission_id=mid, task_id="t-review", validator="review_validator",
        result=VerdictResult.PASS.value,
    )
    log.log_validator_verdict(
        mission_id=mid, task_id="t-behavior", validator="behavior_validator",
        result=VerdictResult.PASS.value,
    )
    log.log_mission_end(
        mission_id=mid, result=result, total_cost_usd=0.40, total_wall_clock_hours=2.5,
    )
    return store


# -- discovery --------------------------------------------------------------


def test_discover_ignores_non_mission_dirs(tmp_path: Path) -> None:
    """A directory is only a mission if it carries the canonical files —
    otherwise stray dirs would pollute the baseline."""
    _clean_pass_mission(tmp_path, "m1")
    (tmp_path / "not_a_mission").mkdir()
    (tmp_path / "not_a_mission" / "README.txt").write_text("hi")

    assert discover_missions(tmp_path) == ["m1"]


def test_discover_empty_root(tmp_path: Path) -> None:
    assert discover_missions(tmp_path / "nope") == []


# -- per-mission derivation -------------------------------------------------


def test_clean_mission_is_first_and_final_pass(tmp_path: Path) -> None:
    """Validators passed, no second pass, mission completed → first AND final pass."""
    m = compute_mission_metrics(_clean_pass_mission(tmp_path, "m1"))
    assert m.first_pass is True
    assert m.completed is True
    assert m.validator_pass_count == 2
    assert m.validator_fail_count == 0
    assert m.cost_usd == 0.40
    assert m.wall_clock_hours == 2.5
    assert m.human_intervention is False


def test_second_pass_trigger_breaks_first_pass(tmp_path: Path) -> None:
    """A second pass means the first attempt was not clean — first_pass must be
    False even if the mission ultimately completes."""
    store = _clean_pass_mission(tmp_path, "m1")
    store.event_log().log_second_pass_triggered(
        mission_id="m1", task_id="t-review", reason="incomplete handoff"
    )
    m = compute_mission_metrics(store)
    assert m.second_pass_count == 1
    assert m.first_pass is False
    assert m.completed is True  # still finished, just not on the first try


def test_validator_fail_breaks_first_pass(tmp_path: Path) -> None:
    """A FAIL verdict disqualifies first_pass and is tallied."""
    store = _store(tmp_path, "m1")
    log = store.event_log()
    log.log_validator_verdict(
        mission_id="m1", task_id="t", validator="review_validator",
        result=VerdictResult.FAIL.value,
    )
    log.log_mission_end(mission_id="m1", result="complete", total_cost_usd=0.0, total_wall_clock_hours=1.0)
    m = compute_mission_metrics(store)
    assert m.validator_fail_count == 1
    assert m.first_pass is False


def test_no_verdicts_is_not_first_pass(tmp_path: Path) -> None:
    """first_pass is only meaningful when validators ran; a mission with none
    is not credited a first pass."""
    store = _clean_pass_mission(tmp_path, "m1")  # has verdicts
    bare = _store(tmp_path, "m2")
    bare.event_log().log_mission_end(
        mission_id="m2", result="complete", total_cost_usd=0.0, total_wall_clock_hours=0.1
    )
    m = compute_mission_metrics(bare)
    assert m.has_validator_verdicts is False
    assert m.first_pass is False


def test_human_gate_escalation_marks_intervention(tmp_path: Path) -> None:
    """An escalation targeting the human gate is what 'human intervention' means."""
    store = _clean_pass_mission(tmp_path, "m1")
    store.event_log().log_escalation(
        mission_id="m1", target="human_gate", reason="review PASS but behavior FAIL"
    )
    m = compute_mission_metrics(store)
    assert m.human_gate_escalations == 1
    assert m.human_intervention is True


def test_non_human_gate_escalation_does_not_count(tmp_path: Path) -> None:
    """Only human_gate escalations are interventions — other targets don't count."""
    store = _clean_pass_mission(tmp_path, "m1")
    store.event_log().log_escalation(mission_id="m1", target="orchestrator", reason="replan")
    m = compute_mission_metrics(store)
    assert m.human_intervention is False


def test_routing_savings_summed(tmp_path: Path) -> None:
    """Smart Router ROUTE_DECISION savings accumulate into routing_savings_usd."""
    store = _clean_pass_mission(tmp_path, "m1")
    log = store.event_log()
    log.log_route_decision(mission_id="m1", task_id="t1", tier="simple", model="cheap", saved_vs_baseline_usd=0.10)
    log.log_route_decision(mission_id="m1", task_id="t2", tier="medium", model="mid", saved_vs_baseline_usd=0.05)
    m = compute_mission_metrics(store)
    assert m.routing_savings_usd == pytest.approx(0.15)


def test_running_mission_has_no_result(tmp_path: Path) -> None:
    """A mission with no MISSION_END is tolerated: result '' and not completed."""
    store = _store(tmp_path, "m1")
    store.event_log().log_mission_start(mission_id="m1", goal="g", repo="/r")
    m = compute_mission_metrics(store)
    assert m.result == ""
    assert m.completed is False


def test_cost_falls_back_to_mission_state(tmp_path: Path) -> None:
    """When the log has no LLM-cost events, the heartbeat-mirrored
    mission_state cost is used so the metric isn't silently zero."""
    store = ArtifactStore(tmp_path, "m1")
    store.save_mission_state(
        MissionState(mission_id="m1", started_at=datetime.now(UTC), cumulative_cost_usd=1.23)
    )
    store.event_log().log_mission_end(
        mission_id="m1", result="complete", total_cost_usd=1.23, total_wall_clock_hours=0.0
    )
    m = compute_mission_metrics(store)
    assert m.cost_usd == 1.23


# -- aggregate baseline -----------------------------------------------------


def test_baseline_aggregates_rates(tmp_path: Path) -> None:
    """3 missions: 2 first-pass+completed, 1 with a second pass that aborted and
    hit the human gate → first_pass 2/3, final 2/3, intervention 1/3."""
    _clean_pass_mission(tmp_path, "m1")
    _clean_pass_mission(tmp_path, "m2")
    bad = _clean_pass_mission(tmp_path, "m3", result="aborted")
    log = bad.event_log()
    log.log_second_pass_triggered(mission_id="m3", task_id="t", reason="r")
    log.log_escalation(mission_id="m3", target="human_gate", reason="stuck")

    report = compute_baseline(tmp_path)
    assert report.mission_count == 3
    assert report.first_pass_rate == 2 / 3
    assert report.final_pass_rate == 2 / 3
    assert report.human_intervention_rate == 1 / 3
    assert report.avg_cost_usd == pytest.approx(0.40)  # all three logged 0.40
    assert report.pr_review_pass_rate is None  # nothing annotated


def test_pr_review_rate_only_over_annotated(tmp_path: Path) -> None:
    """PR-review pass is human-judged: the rate is computed only over missions
    the caller annotates, and is None when none are."""
    _clean_pass_mission(tmp_path, "m1")
    _clean_pass_mission(tmp_path, "m2")
    _clean_pass_mission(tmp_path, "m3")
    report = compute_baseline(tmp_path, pr_review_passed={"m1": True, "m2": False})
    assert report.pr_review_pass_rate == 0.5  # 1 of 2 annotated; m3 ignored
    by_id = {m.mission_id: m for m in report.missions}
    assert by_id["m1"].pr_review_passed is True
    assert by_id["m3"].pr_review_passed is None


def test_baseline_empty(tmp_path: Path) -> None:
    """No missions → zeroed rates, no crash, PR rate None."""
    report = compute_baseline(tmp_path)
    assert report.mission_count == 0
    assert report.first_pass_rate == 0.0
    assert report.avg_cost_usd == 0.0
    assert report.pr_review_pass_rate is None


def test_render_markdown_contains_metrics(tmp_path: Path) -> None:
    _clean_pass_mission(tmp_path, "m1")
    md = render_baseline_markdown(compute_baseline(tmp_path))
    assert "Health Metric Baseline" in md
    assert "First-pass rate" in md
    assert "Missions analyzed: **1**" in md
    assert "n/a (no missions annotated)" in md  # PR rate unannotated
