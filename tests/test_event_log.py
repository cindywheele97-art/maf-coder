"""Tests for EventLog.

Phase A 退出门槛: `pytest tests/test_event_log.py` 全过.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maf_coder.blackboard import Event, EventKind, EventLog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "events.jsonl")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_creates_file_if_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        assert not path.exists()
        EventLog(path)
        assert path.exists()

    def test_creates_parent_dir_lazily(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "events.jsonl"
        EventLog(path)
        assert path.exists()

    def test_does_not_clobber_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        path.write_text('{"existing": "line"}\n')
        EventLog(path)  # constructing should not erase existing content
        assert path.read_text() == '{"existing": "line"}\n'


# ---------------------------------------------------------------------------
# Core append + iterate
# ---------------------------------------------------------------------------


class TestAppendAndIterate:
    def test_single_event_roundtrip(self, log: EventLog) -> None:
        log.log_mission_start(mission_id="m1", goal="add /health")
        events = list(log.iter_events())
        assert len(events) == 1
        assert events[0].kind == EventKind.MISSION_START.value
        assert events[0].payload["goal"] == "add /health"

    def test_multiple_events_preserve_order(self, log: EventLog) -> None:
        log.log_mission_start(mission_id="m1", goal="do thing")
        log.log_task_dispatched(
            mission_id="m1", task_id="t1", owner="coder_worker", priority="medium"
        )
        log.log_task_complete(
            mission_id="m1", task_id="t1", actor="coder_worker", duration_sec=120.5
        )
        events = list(log.iter_events())
        kinds = [e.kind for e in events]
        assert kinds == [
            EventKind.MISSION_START.value,
            EventKind.TASK_DISPATCHED.value,
            EventKind.TASK_COMPLETE.value,
        ]

    def test_iter_empty_log(self, log: EventLog) -> None:
        assert list(log.iter_events()) == []

    def test_malformed_lines_skipped_with_warning(
        self, log: EventLog, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Write one bad line + one good line directly
        log.path.write_text('not json\n{"bad": true}\n')
        log.log_mission_start(mission_id="m1", goal="x")  # this appends good event
        events = list(log.iter_events())
        # Only the well-formed Event should survive (mission_start)
        assert len(events) == 1
        assert events[0].kind == EventKind.MISSION_START.value


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


class TestFiltering:
    def test_filter_by_enum(self, log: EventLog) -> None:
        log.log_mission_start(mission_id="m1", goal="x")
        log.log_llm_call(
            mission_id="m1",
            actor="orchestrator",
            model="anthropic/opus",
            tokens_in=100,
            tokens_out=200,
            cost_usd=0.05,
            latency_sec=2.0,
        )
        log.log_llm_call(
            mission_id="m1",
            actor="coder_worker",
            model="anthropic/sonnet",
            tokens_in=500,
            tokens_out=300,
            cost_usd=0.03,
            latency_sec=4.0,
        )
        llm_events = list(log.filter_kind(EventKind.LLM_CALL))
        assert len(llm_events) == 2
        assert all(e.kind == EventKind.LLM_CALL.value for e in llm_events)

    def test_filter_by_string_also_works(self, log: EventLog) -> None:
        log.log_mission_start(mission_id="m1", goal="x")
        events = list(log.filter_kind("mission_start"))
        assert len(events) == 1


# ---------------------------------------------------------------------------
# Aggregations — the things Status Report + retro need
# ---------------------------------------------------------------------------


class TestAggregations:
    def _populate_llm_calls(self, log: EventLog) -> None:
        log.log_llm_call(
            mission_id="m1",
            actor="orchestrator",
            model="anthropic/opus",
            tokens_in=1000,
            tokens_out=500,
            cost_usd=0.10,
            latency_sec=3.0,
        )
        log.log_llm_call(
            mission_id="m1",
            actor="coder_worker",
            model="anthropic/sonnet",
            tokens_in=5000,
            tokens_out=2000,
            cost_usd=0.30,
            latency_sec=8.0,
        )
        log.log_llm_call(
            mission_id="m1",
            actor="coder_worker",
            model="anthropic/sonnet",
            tokens_in=3000,
            tokens_out=1500,
            cost_usd=0.20,
            latency_sec=6.0,
        )
        log.log_llm_call(
            mission_id="m1",
            actor="review_validator",
            model="openai/gpt-5",
            tokens_in=2000,
            tokens_out=800,
            cost_usd=0.15,
            latency_sec=5.0,
        )

    def test_total_cost(self, log: EventLog) -> None:
        self._populate_llm_calls(log)
        assert log.total_cost_usd() == pytest.approx(0.75)

    def test_total_tokens(self, log: EventLog) -> None:
        self._populate_llm_calls(log)
        ti, to = log.total_tokens()
        assert ti == 11000
        assert to == 4800

    def test_cost_by_actor(self, log: EventLog) -> None:
        self._populate_llm_calls(log)
        by_actor = log.cost_by_actor()
        assert by_actor["orchestrator"] == pytest.approx(0.10)
        assert by_actor["coder_worker"] == pytest.approx(0.50)
        assert by_actor["review_validator"] == pytest.approx(0.15)

    def test_task_outcomes_latest_state(self, log: EventLog) -> None:
        # t1 succeeds, t2 fails, t3 still dispatched
        log.log_task_dispatched(
            mission_id="m1", task_id="t1", owner="coder_worker", priority="medium"
        )
        log.log_task_dispatched(
            mission_id="m1", task_id="t2", owner="coder_worker", priority="high"
        )
        log.log_task_dispatched(
            mission_id="m1", task_id="t3", owner="research_worker", priority="low"
        )
        log.log_task_complete(mission_id="m1", task_id="t1", actor="coder_worker", duration_sec=300)
        log.log_task_failed(
            mission_id="m1",
            task_id="t2",
            actor="coder_worker",
            reason="clippy errors",
            will_retry=True,
        )
        outcomes = log.task_outcomes()
        assert outcomes == {"t1": "complete", "t2": "failed", "t3": "dispatched"}


# ---------------------------------------------------------------------------
# v3.1 second-pass event — emitted when handoff completeness rule fires
# ---------------------------------------------------------------------------


class TestSecondPassEvent:
    def test_log_second_pass_triggered(self, log: EventLog) -> None:
        log.log_second_pass_triggered(
            mission_id="m1",
            task_id="t1",
            reason="handoff has empty incomplete/issues/deviations",
        )
        events = list(log.filter_kind(EventKind.SECOND_PASS_TRIGGERED))
        assert len(events) == 1
        assert events[0].actor == "review_validator"
        assert "empty" in events[0].payload["reason"]


# ---------------------------------------------------------------------------
# last_event helper
# ---------------------------------------------------------------------------


class TestLastEvent:
    def test_last_event_empty(self, log: EventLog) -> None:
        assert log.last_event() is None

    def test_last_event_returns_most_recent(self, log: EventLog) -> None:
        log.log_mission_start(mission_id="m1", goal="x")
        log.log_task_dispatched(
            mission_id="m1", task_id="t1", owner="coder_worker", priority="medium"
        )
        last = log.last_event()
        assert last is not None
        assert last.kind == EventKind.TASK_DISPATCHED.value


# ---------------------------------------------------------------------------
# Direct Event append (for custom events outside the convenience set)
# ---------------------------------------------------------------------------


class TestDirectAppend:
    def test_custom_event(self, log: EventLog) -> None:
        log.append(
            Event(
                kind="custom_phase_a_marker",
                mission_id="m1",
                actor="orchestrator",
                payload={"phase": "A", "milestone": "schema layer done"},
            )
        )
        events = list(log.filter_kind("custom_phase_a_marker"))
        assert len(events) == 1
        assert events[0].payload["phase"] == "A"


# ---------------------------------------------------------------------------
# SR-3 — Smart Router route-decision event
# ---------------------------------------------------------------------------


class TestRouteDecisionEvent:
    def test_appends_well_formed_event_that_roundtrips(self, log: EventLog) -> None:
        # WHY: the route decision must survive disk round-trip with its tier +
        # model + savings intact — that's the entire observability contract SR-3
        # exists for (mission stats --routing reads these back).
        log.log_route_decision(
            mission_id="m1",
            task_id="t1",
            tier="reasoning",
            model="anthropic/claude-opus-4-7",
            saved_vs_baseline_usd=-0.24,
            actor="coder_worker",
        )
        events = list(log.filter_kind(EventKind.ROUTE_DECISION))
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == EventKind.ROUTE_DECISION.value
        assert ev.task_id == "t1"
        assert ev.actor == "coder_worker"
        assert ev.payload["tier"] == "reasoning"
        assert ev.payload["model"] == "anthropic/claude-opus-4-7"
        assert ev.payload["saved_vs_baseline_usd"] == pytest.approx(-0.24)

    def test_savings_none_when_not_computable(self, log: EventLog) -> None:
        # WHY: "not computable" (no cost table) must be recorded as null, not a
        # fabricated zero — downstream sums must be able to distinguish them.
        log.log_route_decision(
            mission_id="m1",
            task_id=None,
            tier="medium",
            model="anthropic/claude-sonnet-4-6",
        )
        ev = next(iter(log.filter_kind(EventKind.ROUTE_DECISION)))
        assert ev.payload["saved_vs_baseline_usd"] is None
        assert ev.task_id is None


# ---------------------------------------------------------------------------
# F1: concurrent appends must not tear lines (process-wide path-keyed lock)
# ---------------------------------------------------------------------------


def test_concurrent_thread_appends_do_not_interleave(tmp_path: Path) -> None:
    """Many threads each open a SEPARATE EventLog on the same file (mirrors
    ArtifactStore.event_log() handing out a fresh instance per call) and append a
    >4KB event concurrently. The shared per-file lock must keep every line intact:
    correct count, all valid JSON, no missing/duplicated indices. Without the lock
    the large lines interleave and the jsonl is corrupt."""
    import json
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "events.jsonl"
    n = 200
    blob = "x" * 8000  # well past PIPE_BUF (~4KB) → interleaving likely without the lock

    def writer(i: int) -> None:
        # New EventLog instance per call — the realistic per-mission pattern.
        EventLog(path).append(
            Event(kind=EventKind.LLM_CALL.value, mission_id="m", payload={"i": i, "blob": blob})
        )

    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(writer, range(n)))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n, f"expected {n} lines, got {len(lines)} (torn/interleaved writes)"
    parsed = [json.loads(line) for line in lines]  # raises if any line is torn
    assert sorted(p["payload"]["i"] for p in parsed) == list(range(n))


def test_event_log_instances_share_one_lock_per_path(tmp_path: Path) -> None:
    """Two EventLog instances on the same file resolve to the SAME append lock
    (so cross-instance appends serialize); a different file gets a different lock."""
    path = tmp_path / "events.jsonl"
    a = EventLog(path)
    b = EventLog(path)
    assert a._append_lock is b._append_lock
    other = EventLog(tmp_path / "other.jsonl")
    assert other._append_lock is not a._append_lock
