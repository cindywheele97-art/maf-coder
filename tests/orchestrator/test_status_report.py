"""Status-report SupervisionHook tests (Phase E E-comms / E2).

Encodes WHY status reporting matters:
- A report fires ONLY when due (interval elapsed since last) — otherwise the
  user would be spammed; not-due ticks must be pure no-ops.
- When it fires it writes BOTH the human .md and machine .json (the user reads
  one, downstream tooling the other), advances last_status_report_at (so the
  next tick is not-due), emits the canonical STATUS_REPORT_EMITTED event, and
  hands the report to the push adapter.
- The webhook adapter POSTs the report JSON via an INJECTED client — never a
  live network call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from maf_coder.blackboard import ArtifactStore
from maf_coder.orchestrator.push import NullPushAdapter, WebhookPushAdapter
from maf_coder.orchestrator.status_report import make_status_report_hook
from maf_coder.orchestrator.supervisor import SupervisionContext
from maf_coder.schemas import MissionState, StatusReport


class _RecordingAdapter(NullPushAdapter):
    def __init__(self) -> None:
        self.sent: list[StatusReport] = []

    async def send(self, report: StatusReport) -> None:
        self.sent.append(report)


def _store(tmp_path: Path, mission_id: str = "m-status") -> ArtifactStore:
    store = ArtifactStore(tmp_path / "missions", mission_id)
    store.save_mission_state(
        MissionState(
            mission_id=mission_id,
            started_at=datetime.now(UTC) - timedelta(hours=5),
            current_milestone="m2",
            completed_milestones=["m1"],
        )
    )
    return store


def _ctx(store: ArtifactStore, *, now: datetime, elapsed_hours: float = 5.0) -> SupervisionContext:
    ms = store.load_mission_state()
    return SupervisionContext(
        mission_id=store.mission_id,
        mission_state=ms,
        elapsed_hours=elapsed_hours,
        total_cost_usd=store.event_log().total_cost_usd(),
        now=now,
        store=store,
        event_log=store.event_log(),
    )


@pytest.mark.asyncio
async def test_fires_when_due_writes_both_files_and_pushes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # An LLM call so budget derivation reflects real event-log totals.
    store.event_log().log_llm_call(
        mission_id=store.mission_id,
        actor="coder_worker",
        model="anthropic/x",
        tokens_in=100,
        tokens_out=200,
        cost_usd=3.5,
        latency_sec=0.1,
    )
    adapter = _RecordingAdapter()
    hook = make_status_report_hook(interval=timedelta(hours=4), push=adapter)

    # last_status_report_at is None -> due.
    now = datetime.now(UTC)
    await hook(_ctx(store, now=now))

    md = store.mission_dir / "status_reports" / "status_0001.md"
    js = store.mission_dir / "status_reports" / "status_0001.json"
    assert md.exists(), "human-readable .md must be written"
    assert js.exists(), "machine-readable .json must be written"

    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["report_number"] == 1
    # Budget mirrors the event log: cost summed, tokens summed.
    assert data["budget_status"]["cost_usd"] == pytest.approx(3.5)
    assert data["budget_status"]["tokens_used"] == 300
    # Milestones mapped: m1 complete, m2 in_progress.
    states = {m["milestone_id"]: m["state"] for m in data["milestones"]}
    assert states == {"m1": "complete", "m2": "in_progress"}

    # last_status_report_at advanced so the next tick is not-due.
    assert store.load_mission_state().last_status_report_at == now

    # STATUS_REPORT_EMITTED event on the canonical log.
    kinds = [e.kind for e in store.event_log().iter_events()]
    assert "status_report_emitted" in kinds

    # Push adapter received the report.
    assert len(adapter.sent) == 1
    assert adapter.sent[0].report_number == 1


@pytest.mark.asyncio
async def test_not_due_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Reported 1 hour ago; interval is 4h -> not due.
    last = datetime.now(UTC) - timedelta(hours=1)
    ms = store.load_mission_state().model_copy(update={"last_status_report_at": last})
    store.save_mission_state(ms)

    adapter = _RecordingAdapter()
    hook = make_status_report_hook(interval=timedelta(hours=4), push=adapter)
    await hook(_ctx(store, now=datetime.now(UTC)))

    assert not (store.mission_dir / "status_reports").exists() or not list(
        (store.mission_dir / "status_reports").iterdir()
    )
    assert adapter.sent == []
    # last_status_report_at untouched.
    assert store.load_mission_state().last_status_report_at == last


@pytest.mark.asyncio
async def test_report_number_increments_across_fires(tmp_path: Path) -> None:
    store = _store(tmp_path)
    adapter = _RecordingAdapter()
    hook = make_status_report_hook(interval=timedelta(seconds=0), push=adapter)

    await hook(_ctx(store, now=datetime.now(UTC)))
    # interval=0 => always due; second fire produces report #2.
    await hook(_ctx(store, now=datetime.now(UTC)))

    assert (store.mission_dir / "status_reports" / "status_0001.json").exists()
    assert (store.mission_dir / "status_reports" / "status_0002.json").exists()
    assert [r.report_number for r in adapter.sent] == [1, 2]


@pytest.mark.asyncio
async def test_push_error_does_not_propagate(tmp_path: Path) -> None:
    store = _store(tmp_path)

    class _Boom(NullPushAdapter):
        async def send(self, report: StatusReport) -> None:
            raise RuntimeError("delivery exploded")

    hook = make_status_report_hook(interval=timedelta(hours=4), push=_Boom())
    # Must not raise — a failed push cannot affect the mission.
    await hook(_ctx(store, now=datetime.now(UTC)))
    # The report itself was still rendered.
    assert (store.mission_dir / "status_reports" / "status_0001.json").exists()


@pytest.mark.asyncio
async def test_webhook_adapter_posts_via_injected_client(tmp_path: Path) -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    async def fake_post(url: str, payload: dict[str, Any]) -> None:
        posts.append((url, payload))

    adapter = WebhookPushAdapter("https://example.test/hook", fake_post)
    report = StatusReport.model_validate(
        {
            "report_number": 7,
            "mission_id": "m-status",
            "mission_started_at": datetime.now(UTC).isoformat(),
            "elapsed_hours": 5.0,
            "milestones": [],
            "current_activity": "x",
            "budget_status": {
                "tokens_used": 0,
                "cost_usd": 0.0,
                "alert_threshold_usd": 50.0,
                "projected_total_usd": 0.0,
                "wall_clock_vs_estimate_pct": 100.0,
            },
        }
    )
    await adapter.send(report)

    assert len(posts) == 1
    url, payload = posts[0]
    assert url == "https://example.test/hook"
    assert payload["report_number"] == 7
    # No real network: the only transport is the injected callable.


@pytest.mark.asyncio
async def test_webhook_adapter_swallows_transport_error(tmp_path: Path) -> None:
    async def boom_post(url: str, payload: dict[str, Any]) -> None:
        raise ConnectionError("network down")

    adapter = WebhookPushAdapter("https://example.test/hook", boom_post)
    report = StatusReport.model_validate(
        {
            "report_number": 1,
            "mission_id": "m",
            "mission_started_at": datetime.now(UTC).isoformat(),
            "elapsed_hours": 1.0,
            "milestones": [],
            "current_activity": "x",
            "budget_status": {
                "tokens_used": 0,
                "cost_usd": 0.0,
                "alert_threshold_usd": 50.0,
                "projected_total_usd": 0.0,
                "wall_clock_vs_estimate_pct": 100.0,
            },
        }
    )
    # A transport failure must not escape the adapter.
    await adapter.send(report)
