"""Cross-mission memory schemas (Phase F — F-memory).

These models describe rows persisted in the per-repo memory store and the
configurable global-lessons store, plus the structured mission retro that is
the primary source of new memory entries.

Design (MAF-Coder_v2_Build_Plan §Phase F):
- A `MemoryRecord` is one persisted experience: a retro section, a contract
  excerpt, a handoff summary, or a project profile note — tagged so retrieval
  can score by goal / topic / module overlap.
- A `Lesson` is a `MemoryRecord` that has been promoted to the global store
  (cross-repo). Only records flagged `global_lesson=True` are eligible.
- A `RetroEntry` is the assembled mission_retro.md payload: what worked / what
  failed / surprises / candidate global lessons. The retro is decomposed into
  `MemoryRecord` rows on ingest.

All models use `extra="forbid"` (soul.md §1 invariant) — an unexpected key is a
bug, not data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class MemoryKind(str, Enum):
    """Vocabulary for the `kind` column. Treat as a vocabulary, not a closed set.

    Adding a kind is non-breaking; the store column is a plain string.
    """

    RETRO = "retro"
    CONTRACT = "contract"
    HANDOFF = "handoff"
    PROFILE = "profile"
    LESSON = "lesson"


class MemoryRecord(BaseModel):
    """One persisted experience row in a memory store.

    `tags` carry the searchable surface: goal keywords, topic, and module tags.
    Retrieval scores token overlap between the query and `text` + `tags`.

    `global_lesson` marks a record as eligible for promotion into the global
    lessons store (F4). Records not so flagged stay project-local.
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    record_id: str = Field(description="Stable id, unique within a store")
    mission_id: str = Field(description="Source mission that produced this record")
    kind: str = Field(description="One of MemoryKind values (free-form tolerated)")
    text: str = Field(description="The lesson / summary body — the retrievable content")
    tags: list[str] = Field(
        default_factory=list,
        description="goal/topic/module keywords, lowercased on ingest for scoring",
    )
    module: str | None = Field(
        default=None, description="Optional module/component tag for filtered retrieval"
    )
    global_lesson: bool = Field(
        default=False,
        description="If True, eligible for promotion into the global lessons store (F4)",
    )
    quarantined: bool = Field(
        default=False,
        description="If True, excluded from retrieval (anti-poisoning, F3)",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Lesson(BaseModel):
    """A globally-promoted lesson (cross-repo).

    Structurally a slimmer `MemoryRecord`: global lessons drop the per-mission
    module binding because they are meant to generalize across repos.
    """

    model_config = ConfigDict(extra="forbid")

    lesson_id: str = Field(description="Stable id, unique within the global store")
    source_mission_id: str = Field(description="Mission that first produced the lesson")
    text: str = Field(description="The generalized lesson body")
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RetroEntry(BaseModel):
    """Structured mission retrospective (F6).

    Assembled at mission end from the EventLog + artifacts, then decomposed into
    `MemoryRecord` rows on ingest. `global_lessons` are the subset promoted to
    the global store.
    """

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    goal: str = Field(description="One-line restatement of the mission goal")
    what_worked: list[str] = Field(default_factory=list)
    what_failed: list[str] = Field(default_factory=list)
    surprises: list[str] = Field(default_factory=list)
    global_lessons: list[str] = Field(
        default_factory=list,
        description="Lessons general enough to persist across repos (F4)",
    )
    modules: list[str] = Field(
        default_factory=list, description="Modules/components this mission touched"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
