"""Blackboard layer — file-backed artifact store + append-only event log.

Public API:
    ArtifactStore          — type-safe mission artifact read/write
    EventLog               — append-only jsonl event stream
    Event, EventKind       — event schema + canonical kinds vocabulary
    ArtifactStoreError     — base exception type
    PathEscapeError        — relpath traversal attempt
    ContractAlreadyLockedError — violated soul.md §2 write-once contract rule
"""

from .artifact_store import (
    ArtifactStore,
    ArtifactStoreError,
    ContractAlreadyLockedError,
    PathEscapeError,
)
from .event_log import Event, EventKind, EventLog

__all__ = [
    "ArtifactStore",
    "ArtifactStoreError",
    "ContractAlreadyLockedError",
    "Event",
    "EventKind",
    "EventLog",
    "PathEscapeError",
]
