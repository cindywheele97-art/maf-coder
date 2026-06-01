"""ProjectMemory + GlobalLessons store tests (Phase F — F1, F4).

These tests encode WHY the store matters: a round-trip must preserve every
field (so retrieval scores on real data), quarantine must hide a poisoned row,
and the global store must ingest ONLY flagged records and collapse near-dupes
(so the cross-repo store doesn't drown in noise or accept project-local rows).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from maf_coder.memory.store import (
    DEDUP_THRESHOLD,
    GlobalLessons,
    ProjectMemory,
)
from maf_coder.schemas import Lesson, MemoryRecord


def _rec(record_id: str, **kw: object) -> MemoryRecord:
    base: dict[str, object] = {
        "record_id": record_id,
        "mission_id": "m1",
        "kind": "retro",
        "text": "lesson body",
        "tags": ["alpha"],
    }
    base.update(kw)
    return MemoryRecord(**base)  # type: ignore[arg-type]


def test_project_db_lives_under_repo_dot_maf_coder(tmp_path: Path) -> None:
    pm = ProjectMemory(tmp_path)
    assert pm.db_path == tmp_path / ".maf-coder" / "memory.db"
    assert pm.db_path.exists()
    pm.close()


def test_insert_roundtrip_preserves_all_fields(tmp_path: Path) -> None:
    pm = ProjectMemory(tmp_path)
    created = datetime(2026, 1, 1, tzinfo=UTC)
    rec = _rec(
        "r1",
        kind="handoff",
        text="prefer parameterized queries",
        tags=["SQL", "Security"],
        module="db",
        global_lesson=True,
        created_at=created,
    )
    pm.insert(rec)

    got = pm.get("r1")
    assert got is not None
    assert got.text == "prefer parameterized queries"
    # tags are lowercased on ingest (so retrieval scoring is case-stable)
    assert got.tags == ["sql", "security"]
    assert got.module == "db"
    assert got.global_lesson is True
    assert got.created_at == created
    pm.close()


def test_insert_or_replace_is_idempotent_on_record_id(tmp_path: Path) -> None:
    pm = ProjectMemory(tmp_path)
    pm.insert(_rec("r1", text="v1"))
    pm.insert(_rec("r1", text="v2"))
    assert pm.count() == 1
    got = pm.get("r1")
    assert got is not None
    assert got.text == "v2"
    pm.close()


def test_quarantine_excludes_from_default_reads(tmp_path: Path) -> None:
    pm = ProjectMemory(tmp_path)
    pm.insert(_rec("r1"))
    assert pm.quarantine("r1") is True
    # default read hides it (anti-poisoning) ...
    assert [r.record_id for r in pm.all_records()] == []
    # ... but it is still on disk when explicitly requested
    assert [r.record_id for r in pm.all_records(include_quarantined=True)] == ["r1"]
    pm.close()


def test_quarantine_unknown_id_returns_false(tmp_path: Path) -> None:
    pm = ProjectMemory(tmp_path)
    assert pm.quarantine("nope") is False
    pm.close()


def test_global_ingest_only_flagged_records(tmp_path: Path) -> None:
    gl = GlobalLessons(tmp_path / "global.db")
    local = _rec("r1", global_lesson=False)
    promoted = _rec("r2", global_lesson=True, text="always lock the contract first")

    assert gl.ingest_record(local) is False
    assert gl.ingest_record(promoted) is True
    assert gl.count() == 1
    assert gl.all_lessons()[0].text == "always lock the contract first"
    gl.close()


def test_global_dedup_collapses_near_duplicates(tmp_path: Path) -> None:
    gl = GlobalLessons(tmp_path / "global.db")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    # Three near-identical lessons + one distinct one.
    for i in range(3):
        gl.insert(
            Lesson(
                lesson_id=f"l{i}",
                source_mission_id="m1",
                text="always lock the validation contract before coding starts",
                tags=["contract"],
                created_at=t0 + timedelta(seconds=i),
            )
        )
    gl.insert(
        Lesson(
            lesson_id="distinct",
            source_mission_id="m2",
            text="rotate exposed api secrets immediately on discovery",
            tags=["security"],
            created_at=t0 + timedelta(seconds=10),
        )
    )

    removed = gl.dedup()
    assert removed == 2  # two of the three near-dupes collapse
    ids = {lsn.lesson_id for lsn in gl.all_lessons()}
    # oldest of the cluster survives + the distinct lesson
    assert ids == {"l0", "distinct"}
    gl.close()


def test_global_dedup_threshold_constant_is_sane() -> None:
    # F4: dedup fires once the lessons count crosses ~50. Pin the contract so a
    # silent change to the threshold is caught.
    assert DEDUP_THRESHOLD == 50
