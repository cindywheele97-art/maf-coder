"""user_messages/ inbox primitives + a polling SupervisionHook (Phase E E3).

Why this exists:
    soul.md §5.2: the user steers a running mission by dropping ``.md`` files
    into ``user_messages/``; the Orchestrator polls at milestone boundaries.
    ``!urgent``-prefixed files are surfaced immediately rather than waiting.

    The read/parse/archive logic was first written inline in the
    ``poll_user_messages`` / ``mark_user_message_processed`` orchestrator tools.
    This module is the single source of truth for that logic: the tools now
    delegate here, and the supervision hook reuses the *same* functions so there
    is exactly one implementation of "read the inbox" and "archive a message".

Design:
    - ``read_inbox_entries(store)`` — list unprocessed messages, urgent first.
    - ``archive_message(store, filename)`` — move user_messages/<f> ->
      processed_messages/<f> and stamp mission_state.last_user_message_processed_at.
    - ``make_inbox_poll_hook(...)`` — SupervisionHook that polls each due tick:
      ``!urgent`` messages are surfaced immediately (USER_MESSAGE_RECEIVED event
      + archived); normal messages are recorded (event) and left in place for
      milestone-boundary processing by the Orchestrator agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..blackboard import ArtifactStore
from .supervisor import SupervisionContext, SupervisionHook

logger = logging.getLogger(__name__)

# How often the supervision hook polls the inbox. Smaller than the status
# interval — steering messages should be noticed promptly. Injectable for tests.
DEFAULT_INBOX_POLL_INTERVAL = timedelta(minutes=15)


@dataclass(frozen=True)
class InboxEntry:
    """One unprocessed user message in the inbox."""

    filename: str
    path: str
    content: str
    urgent: bool
    created_at: str


def read_inbox_entries(store: ArtifactStore) -> list[InboxEntry]:
    """Return unprocessed user messages, urgent first then oldest-first.

    Mirrors the original ``poll_user_messages`` read/parse exactly: skips
    non-``.md`` files and ``_pending_`` escalation stubs; ``!urgent`` prefix in
    the filename marks urgency.
    """
    entries: list[InboxEntry] = []
    for p in store.list_dir("user_messages"):
        if not p.is_file() or not p.name.endswith(".md"):
            continue
        if p.name.startswith("_pending_"):
            continue
        urgent = p.name.startswith("!urgent")
        entries.append(
            InboxEntry(
                filename=p.name,
                path=f"user_messages/{p.name}",
                content=p.read_text(encoding="utf-8"),
                urgent=urgent,
                created_at=datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat(),
            )
        )
    entries.sort(key=lambda e: (not e.urgent, e.created_at))
    return entries


def archive_message(store: ArtifactStore, filename: str) -> None:
    """Move user_messages/<filename> -> processed_messages/<filename>.

    Stamps mission_state.last_user_message_processed_at (immutably) if state
    exists. Single source of truth for ``mark_user_message_processed``.
    """
    src = f"user_messages/{filename}"
    if not store.exists(src):
        raise FileNotFoundError(f"archive_message: {src}: not found")
    content = store.read_text(src)
    store.write_text(f"processed_messages/{filename}", content)
    try:
        (store.mission_dir / "user_messages" / filename).unlink()
    except OSError as e:
        raise OSError(f"archive_message: unlink {src}: {e}") from e
    _stamp_processed_at(store)


def _stamp_processed_at(store: ArtifactStore) -> None:
    """Update mission_state.last_user_message_processed_at immutably, if present."""
    try:
        ms = store.load_mission_state()
    except FileNotFoundError:
        return
    refreshed = ms.model_copy(update={"last_user_message_processed_at": datetime.now(UTC)})
    store.save_mission_state(refreshed)


def _is_due(ctx: SupervisionContext, interval: timedelta) -> bool:
    """Due if the inbox was never polled, or interval has elapsed."""
    last = ctx.mission_state.last_user_message_processed_at
    if last is None:
        return True
    return (ctx.now - last) >= interval


def make_inbox_poll_hook(
    *,
    interval: timedelta = DEFAULT_INBOX_POLL_INTERVAL,
) -> SupervisionHook:
    """Build the user_messages polling SupervisionHook.

    On each due tick it reads the inbox. For every message it emits a
    USER_MESSAGE_RECEIVED event so the arrival is on the canonical log.
    ``!urgent`` messages are surfaced immediately and archived (the Orchestrator
    acts on the event); normal messages are recorded but left in place for the
    Orchestrator to process at the next milestone boundary.

    Always stamps last_user_message_processed_at so the poll cadence advances
    even when the inbox is empty.
    """

    async def inbox_poll_hook(ctx: SupervisionContext) -> None:
        if not _is_due(ctx, interval):
            return

        entries = read_inbox_entries(ctx.store)
        for entry in entries:
            ctx.event_log.log_user_message_received(
                mission_id=ctx.mission_id,
                message_path=entry.path,
                urgent=entry.urgent,
            )
            if entry.urgent:
                # Surface immediately: archive now so it is not re-emitted, and
                # the USER_MESSAGE_RECEIVED event drives the Orchestrator to act.
                try:
                    archive_message(ctx.store, entry.filename)
                except (FileNotFoundError, OSError) as e:
                    logger.warning(
                        "inbox_poll_hook: failed to archive urgent %s: %r",
                        entry.filename,
                        e,
                    )
            # Normal messages are intentionally left in user_messages/ for the
            # Orchestrator agent to process at the next milestone boundary.

        # Advance the poll cadence (and stamp even on empty inbox / no urgents).
        refreshed = ctx.mission_state.model_copy(
            update={"last_user_message_processed_at": ctx.now}
        )
        ctx.store.save_mission_state(refreshed)

    return inbox_poll_hook


__all__ = [
    "DEFAULT_INBOX_POLL_INTERVAL",
    "InboxEntry",
    "archive_message",
    "make_inbox_poll_hook",
    "read_inbox_entries",
]
