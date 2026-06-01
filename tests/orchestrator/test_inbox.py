"""user_messages inbox SupervisionHook tests (Phase E E-comms / E3).

Encodes WHY the inbox matters:
- The user steers a running mission by dropping .md files in user_messages/.
- ``!urgent`` messages must be surfaced IMMEDIATELY (event + archived now) so the
  Orchestrator acts without waiting for a milestone boundary.
- Normal messages are recorded (event) but LEFT IN PLACE for milestone-boundary
  processing — archiving them in the hook would steal them from the Orchestrator.
- Every due poll advances last_user_message_processed_at so the cadence moves
  even on an empty inbox.
- The hook reuses the SAME read/archive primitives as the orchestrator tools.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from maf_coder.blackboard import ArtifactStore
from maf_coder.orchestrator.inbox import (
    make_inbox_poll_hook,
    read_inbox_entries,
)
from maf_coder.orchestrator.supervisor import SupervisionContext
from maf_coder.schemas import MissionState


def _store(tmp_path: Path, mission_id: str = "m-inbox") -> ArtifactStore:
    store = ArtifactStore(tmp_path / "missions", mission_id)
    store.save_mission_state(
        MissionState(mission_id=mission_id, started_at=datetime.now(UTC))
    )
    return store


def _ctx(store: ArtifactStore, *, now: datetime) -> SupervisionContext:
    return SupervisionContext(
        mission_id=store.mission_id,
        mission_state=store.load_mission_state(),
        elapsed_hours=1.0,
        total_cost_usd=0.0,
        now=now,
        store=store,
        event_log=store.event_log(),
    )


@pytest.mark.asyncio
async def test_normal_message_recorded_but_left_in_place(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_text("user_messages/note.md", "please prioritise the health endpoint")

    hook = make_inbox_poll_hook(interval=timedelta(0))
    now = datetime.now(UTC)
    await hook(_ctx(store, now=now))

    # Recorded on the canonical log.
    received = [e for e in store.event_log().iter_events() if e.kind == "user_message_received"]
    assert len(received) == 1
    assert received[0].payload["urgent"] is False

    # Left in place for milestone-boundary processing by the Orchestrator.
    assert store.exists("user_messages/note.md")
    assert not store.exists("processed_messages/note.md")

    # Cadence advanced.
    assert store.load_mission_state().last_user_message_processed_at == now


@pytest.mark.asyncio
async def test_urgent_message_surfaced_and_archived_immediately(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_text("user_messages/!urgent-stop.md", "stop spending immediately")

    hook = make_inbox_poll_hook(interval=timedelta(0))
    await hook(_ctx(store, now=datetime.now(UTC)))

    received = [e for e in store.event_log().iter_events() if e.kind == "user_message_received"]
    assert len(received) == 1
    assert received[0].payload["urgent"] is True

    # Urgent message archived NOW so it is not re-emitted next tick.
    assert not store.exists("user_messages/!urgent-stop.md")
    assert store.exists("processed_messages/!urgent-stop.md")


@pytest.mark.asyncio
async def test_not_due_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    last = datetime.now(UTC) - timedelta(minutes=1)
    store.save_mission_state(
        store.load_mission_state().model_copy(
            update={"last_user_message_processed_at": last}
        )
    )
    store.write_text("user_messages/note.md", "hi")

    hook = make_inbox_poll_hook(interval=timedelta(minutes=15))
    await hook(_ctx(store, now=datetime.now(UTC)))

    # Nothing polled; timestamp untouched.
    assert store.load_mission_state().last_user_message_processed_at == last
    received = [e for e in store.event_log().iter_events() if e.kind == "user_message_received"]
    assert received == []


@pytest.mark.asyncio
async def test_empty_inbox_still_advances_cadence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    hook = make_inbox_poll_hook(interval=timedelta(0))
    now = datetime.now(UTC)
    await hook(_ctx(store, now=now))
    assert store.load_mission_state().last_user_message_processed_at == now


def test_read_inbox_entries_orders_urgent_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_text("user_messages/normal.md", "a")
    store.write_text("user_messages/!urgent-x.md", "b")
    store.write_text("user_messages/_pending_123.md", "escalation stub")

    entries = read_inbox_entries(store)
    # _pending_ stubs are skipped; urgent first.
    names = [e.filename for e in entries]
    assert names == ["!urgent-x.md", "normal.md"]
    assert entries[0].urgent is True
    assert entries[1].urgent is False
