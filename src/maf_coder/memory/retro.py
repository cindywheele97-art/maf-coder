"""Mission retro template + assembler + ingest (Phase F — F6).

The retro is the bridge between one mission's experience and the next mission's
context. Three responsibilities:

1. `render_retro_markdown(entry)` — the structured mission_retro.md template:
   goal / what worked / what failed / surprises / global lessons.
2. `assemble_retro(...)` — draft a `RetroEntry` from the EventLog (+ optional
   handoffs/profile via the ArtifactStore). The drafting is deterministic
   (counts failures, surfaces failed tasks); narrative judgement is the
   Orchestrator's to refine before saving — code does the boring extraction.
3. `ingest_retro(...)` — decompose a `RetroEntry` into `MemoryRecord` rows in
   ProjectMemory, and promote `global_lessons` into the GlobalLessons store.

No LLM calls here; this module is pure assembly + persistence so it is fully
hermetic in tests.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from ..schemas import MemoryKind, MemoryRecord, RetroEntry
from .store import GlobalLessons, ProjectMemory

if TYPE_CHECKING:
    from ..blackboard.event_log import EventLog


class _EventLike(Protocol):
    kind: str
    task_id: str | None
    actor: str | None
    payload: dict[str, object]


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------


def render_retro_markdown(entry: RetroEntry) -> str:
    """Render a RetroEntry as the canonical mission_retro.md document."""

    def _section(title: str, items: Iterable[str]) -> list[str]:
        out = [f"## {title}"]
        items = list(items)
        if items:
            out.extend(f"- {it}" for it in items)
        else:
            out.append("- (none recorded)")
        out.append("")
        return out

    lines = [
        f"# Mission Retro — {entry.mission_id}",
        f"_Created: {entry.created_at.isoformat()}_",
        "",
        "## Goal",
        entry.goal or "(not recorded)",
        "",
    ]
    lines += _section("What Worked", entry.what_worked)
    lines += _section("What Failed", entry.what_failed)
    lines += _section("Surprises", entry.surprises)
    lines += _section("Global Lessons", entry.global_lessons)
    if entry.modules:
        lines += _section("Modules Touched", entry.modules)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------


def assemble_retro(
    *,
    mission_id: str,
    goal: str,
    event_log: EventLog,
    extra_worked: Iterable[str] = (),
    extra_failed: Iterable[str] = (),
    extra_surprises: Iterable[str] = (),
    global_lessons: Iterable[str] = (),
    modules: Iterable[str] = (),
    now: datetime | None = None,
) -> RetroEntry:
    """Draft a RetroEntry from the EventLog plus caller-supplied narrative.

    Deterministic extraction from events:
    - failed tasks (TASK_FAILED) → `what_failed`
    - completed tasks (TASK_COMPLETE) → `what_worked` (count summary)
    - second-pass triggers / validator chain blocks → `surprises`

    Caller-supplied `extra_*` lists let the Orchestrator (or a test) layer in
    narrative judgement on top of the mechanical extraction.
    """
    now = now or datetime.now(UTC)

    completed: list[str] = []
    failed: list[str] = []
    surprises: list[str] = []

    for e in event_log.iter_events():
        if e.kind == "task_complete" and e.task_id:
            completed.append(e.task_id)
        elif e.kind == "task_failed" and e.task_id:
            reason = str(e.payload.get("reason", "")) if e.payload else ""
            failed.append(f"task {e.task_id} failed: {reason}".strip())
        elif e.kind == "second_pass_triggered" and e.task_id:
            surprises.append(f"handoff completeness second-pass fired on {e.task_id}")
        elif e.kind == "validator_chain_blocked" and e.task_id:
            surprises.append(f"dual-validator chain blocked dispatch of {e.task_id}")

    what_worked: list[str] = list(extra_worked)
    if completed:
        what_worked.append(f"{len(completed)} task(s) completed: {', '.join(sorted(set(completed)))}")

    what_failed = list(extra_failed) + failed
    all_surprises = list(extra_surprises) + surprises

    return RetroEntry(
        mission_id=mission_id,
        goal=goal,
        what_worked=what_worked,
        what_failed=what_failed,
        surprises=all_surprises,
        global_lessons=list(global_lessons),
        modules=list(modules),
        created_at=now,
    )


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def _record_id(mission_id: str, slot: str, idx: int) -> str:
    return f"{mission_id}:{slot}:{idx}"


def ingest_retro(
    entry: RetroEntry,
    memory: ProjectMemory,
    *,
    global_lessons: GlobalLessons | None = None,
) -> list[MemoryRecord]:
    """Decompose a RetroEntry into MemoryRecord rows and persist them.

    One row per retro bullet, tagged with the entry's modules + a slot tag
    (worked / failed / surprise / lesson). Each `global_lessons` bullet is
    flagged `global_lesson=True` and, if a GlobalLessons store is provided,
    promoted into it (F4 — flagged-only ingest + opportunistic dedup).

    Returns the records written (useful for assertions in tests).
    """
    module = entry.modules[0] if entry.modules else None
    module_tags = [m.lower() for m in entry.modules]
    goal_tags = entry.goal.lower().split() if entry.goal else []
    base_tags = module_tags + goal_tags

    records: list[MemoryRecord] = []

    def _emit(slot: str, texts: list[str], *, is_global: bool) -> None:
        for i, text in enumerate(texts):
            rec = MemoryRecord(
                record_id=_record_id(entry.mission_id, slot, i),
                mission_id=entry.mission_id,
                kind=MemoryKind.LESSON.value if is_global else MemoryKind.RETRO.value,
                text=text,
                tags=[*base_tags, slot],
                module=module,
                global_lesson=is_global,
                created_at=entry.created_at,
            )
            records.append(rec)

    _emit("worked", entry.what_worked, is_global=False)
    _emit("failed", entry.what_failed, is_global=False)
    _emit("surprise", entry.surprises, is_global=False)
    _emit("lesson", entry.global_lessons, is_global=True)

    memory.insert_many(records)

    if global_lessons is not None:
        for rec in records:
            if rec.global_lesson:
                global_lessons.ingest_record(rec)

    return records


__all__ = [
    "assemble_retro",
    "ingest_retro",
    "render_retro_markdown",
]
